"""Versioned customer cancellation policy. No channel calls or spendable credits.

All transitions serialize on the order row. Refunds use the existing verified
original-payment pipeline; retained money stays in a reconciliation hold.
"""
import hashlib
import json
import math
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import ProviderOrder as Order, ProviderOrderPaymentOrder as Payment
from .models import ProviderOrderRefundOrder as Refund, ProviderOrderSettlement as Settlement

MODES = {"taxi": "出租车", "ride_hailing": "网约车", "bus": "公交", "subway": "地铁"}
DEFAULTS = {"transit_minutes": 20, "transit_early_amount": 3000, "transit_late_amount": 8000,
            "wait_minutes": 20, "no_show_amount": 5000, "arrived_penalty_percent": 30}
SALT = "provider-cancellation-v1"
ELIGIBLE = (Order.Status.PENDING_ACCEPTANCE, Order.Status.PENDING_SERVICE,
            Order.Status.DEPARTED, Order.Status.IN_SERVICE)


def validate_config(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValidationError("取消规则包含无效参数。")
    config = {**DEFAULTS, **value}
    for key, number in config.items():
        lower, upper = (1, 180) if key.endswith("minutes") else (0, 100 if key.endswith("percent") else 100000)
        if type(number) is not int or not lower <= number <= upper:
            raise ValidationError(f"取消规则 {key} 必须为 {lower}–{upper} 的整数。")
    if config["transit_early_amount"] > config["transit_late_amount"]:
        raise ValidationError("超时空单费不能低于时限内空单费。")
    return config


def current_policy():
    from backoffice.operation_settings import platform_operation_rules
    rules = platform_operation_rules()
    if not rules["provider_cancellation_enabled"]:
        return {}
    config = validate_config(rules["provider_cancellation_config"])
    version = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    return {"version": f"cancellation-v1-{version}", "config": config, "clauses": clauses(config)}


def clauses(c):
    return [
        "达人未出发：全额退还本单实付费用。",
        "出租车／网约车，已出发未到达：扣除下单已支付的往返交通费，其余退还。",
        f"公交／地铁，已出发未到达：出发后 {c['transit_minutes']} 分钟内（含）扣 ¥{c['transit_early_amount']/100:g}；超过则扣 ¥{c['transit_late_amount']/100:g}。此费用已含出行补偿，不另加路费。",
        f"已到达、服务未开始，因用户个人原因取消：扣除已支付的往返交通费及实付服务费的 {c['arrived_penalty_percent']}%，其余退还。",
        f"达人到达后联系不上用户：发起等待并通知用户，至少等待 {c['wait_minutes']} 分钟（不早于预约开始计时）。仍未联系且无争议时取消，扣除已支付往返交通费及 ¥{c['no_show_amount']/100:g} 空单费。",
        "服务已开始，因用户个人原因提前结束：服务费不退，已支付往返交通费不退。达人责任、服务质量、安全或责任争议请申请售后，由客服核查，不直接套用此条。",
        "每次只适用一条规则，不叠加扣费；最高不超过本单实付金额，不另扣余额。优惠先抵服务费、再抵其他费用、最后抵交通费，按优惠后实付明细计算退款。",
        "退款按原支付路径处理，以实际到账为准；对到场、联系情况或扣费有异议，可通过订单客服入口提交工单。",
    ]


def booking_policy(data):
    policy = current_policy()
    if (data.get("cancellation_policy_version", "") != policy.get("version", "")
            or (policy and data.get("transport_mode") not in MODES)):
        raise ValidationError("请选择出行方式并重新阅读、同意当前取消规则。")
    return {**policy, "agreed_at": timezone.now().isoformat()} if policy else {}


def calculate(order, *, now, no_show=False):
    from .services import _discounted_order_components
    components = _discounted_order_components(order)
    total = sum(components.values())
    if total != order.payable_amount:
        raise ValidationError("订单金额明细不一致，请联系客服核查。")
    c = order.cancellation_policy["config"]
    retained = {key: 0 for key in components}
    travel, compensation, service = 0, 0, 0
    if no_show:
        rule, label = "no_show", "到场等待后用户未联系"
        travel = retained["transport"] = components["transport"]
        compensation = min(c["no_show_amount"], total - travel)
    elif order.service_started_at:
        rule, label = "service_started", "服务开始后用户个人原因终止"
        travel = retained["transport"] = components["transport"]
        service = retained["service"] = components["service"]
    elif order.arrived_at:
        rule, label = "arrived", "到达后用户个人原因取消"
        travel = retained["transport"] = components["transport"]
        compensation = int((Decimal(components["service"]) * c["arrived_penalty_percent"] / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    elif order.departed_at and order.transport_mode in ("bus", "subway"):
        early = now <= order.departed_at + timedelta(minutes=c["transit_minutes"])
        rule, label = ("transit_early", "公交／地铁时限内取消") if early else ("transit_late", "公交／地铁超时取消")
        compensation = min(c["transit_early_amount"] if early else c["transit_late_amount"], total)
    elif order.departed_at:
        rule, label = "departed", "出租车／网约车出发后取消"
        travel = retained["transport"] = components["transport"]
    else:
        rule, label = "before_departure", "出发前取消"
    remaining = compensation
    for key in ("service", "other", "transport"):
        amount = min(remaining, components[key] - retained[key])
        retained[key] += amount
        remaining -= amount
    refunds = {key: components[key] - retained[key] for key in components}
    return {"rule": rule, "label": label, "paid_amount": total, "refund_amount": sum(refunds.values()),
            "retained_travel_amount": travel, "compensation_amount": compensation,
            "retained_service_amount": service, "component_refunds": refunds,
            "retained_amount": total - sum(refunds.values())}


def ensure_eligible(order):
    from .distributions import assert_refund_not_distributed
    from .timeouts import _manual_intervention_reason
    if not order.cancellation_policy or order.transport_mode not in MODES:
        raise ValidationError("本单使用原规则，请联系客服处理取消。")
    if order.status not in ELIGIBLE or order.cancellation_record:
        raise ValidationError("订单状态已变化，请刷新订单。")
    reason = _manual_intervention_reason(order)
    if reason or order.refund_orders.exists():
        raise ValidationError(f"{reason or '订单已有退款记录'}，请联系客服处理。")
    if order.customer_wait.get("state") == "review":
        raise ValidationError("到场等待已转客服，请在原工单中处理。")
    payment = Payment.objects.filter(order=order).first()
    if (not payment or payment.status != Payment.Status.PAID
            or payment.payable_amount != order.payable_amount or payment.payer_id != order.customer_id
            or not order.paid_at or not payment.paid_at):
        raise ValidationError("支付结果尚未核实，不能按取消规则扣费。")
    if Settlement.objects.filter(order=order, status=Settlement.Status.SETTLED).exists():
        raise ValidationError("本单已结算，请转财务核查。")
    assert_refund_not_distributed(order)


def quote_context(order, amounts):
    return {"order": order.order_no, "customer": order.customer_id, "policy": order.cancellation_policy["version"],
            "status": order.status, "amounts": amounts,
            "departed_at": str(order.departed_at), "arrived_at": str(order.arrived_at),
            "service_started_at": str(order.service_started_at), "mode": order.transport_mode}


@transaction.atomic
def preview(*, order_no, customer):
    order = get_object_or_404(Order.objects.select_for_update(), order_no=order_no, customer=customer)
    ensure_eligible(order)
    amounts = calculate(order, now=timezone.now())
    return {**amounts, "token": signing.dumps(quote_context(order, amounts), salt=SALT),
            "notice": "仅适用于用户个人原因取消。如有达人责任或服务争议，请选择联系客服，不要确认扣费。"}


def reconcile(order):
    """Order lock held. Final refund truth first; never enable retained payouts."""
    from .settlement_plans import sync_provider_settlement_plan
    decision = order.cancellation_record
    refunds = list(order.refund_orders.all())
    if not decision or any(r.status != Refund.Status.SUCCEEDED for r in refunds):
        return
    refunded = sum(r.refund_amount for r in refunds)
    if refunded != decision["refund_amount"]:
        return  # Further support refunds need manual re-adjudication.
    basis = decision["compensation_amount"] + decision["retained_service_amount"]
    rate = Decimal(decision["platform_commission_rate"])
    platform = int((basis * rate / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    now = timezone.now()
    retained = decision["retained_amount"]
    Settlement.objects.update_or_create(order=order, defaults={
        "provider": order.provider, "paid_amount": order.payable_amount, "refunded_amount": refunded,
        "net_service_fee_amount": basis, "net_transport_fee_amount": decision["retained_travel_amount"],
        "net_other_fee_amount": 0, "platform_commission_rate": rate,
        "platform_commission_amount": platform, "provider_service_income_amount": basis - platform,
        "provider_settlement_amount": retained - platform,
        "calculation_snapshot": {"version": SALT, "commission_basis": "retained_service_and_compensation",
                                 "decision": decision, "transport_destination": "provider"},
        "frozen_at": now, "freeze_until": now,
        "status": Settlement.Status.DISPUTE_FROZEN if retained else Settlement.Status.CANCELLED,
        "dispute_reason": "取消订单剩余款待核账，尚未开放自动分账" if retained else "",
        "cancelled_at": None if retained else now,
    })
    sync_provider_settlement_plan(order_no=order.order_no)


def apply_decision(order, amounts, *, now, token="", actor=None):
    from .termination import original_commission_rate
    from .services import create_provider_order_refund
    from .fulfillment import suspend_fulfillment_tasks
    from .timeouts import _notify
    from notifications.models import UserNotification
    rate = original_commission_rate(order, refund_amount=amounts["refund_amount"])
    order.cancellation_record = {**amounts, "version": SALT, "decided_at": now.isoformat(),
                                 "platform_commission_rate": str(rate),
                                 "request_digest": hashlib.sha256(token.encode()).hexdigest() if token else "",
                                 "actor_id": actor.pk if actor else None}
    order.status = Order.Status.TERMINATED if order.service_started_at else Order.Status.CANCELLED
    order.cancelled_at = now
    order.confirmation_expires_at = None
    if order.customer_wait:
        order.customer_wait = {**order.customer_wait, "state": "closed", "closed_at": now.isoformat()}
    order.save()
    suspend_fulfillment_tasks(order)
    if amounts["refund_amount"]:
        create_provider_order_refund(order_no=order.order_no, amount=amounts["refund_amount"],
            source_type=Refund.SourceType.SYSTEM, source_reference=f"cancel:{order.order_no}",
            idempotency_key=f"policy-cancel:{order.order_no}", reason=amounts["label"],
            operator=actor, components_override=amounts["component_refunds"])
    else:
        reconcile(order)
    _notify(order, event=UserNotification.EventType.ORDER_AFTER_SALES_RESULT,
            title="订单已取消" if not order.service_started_at else "服务已提前结束",
            content=f"{amounts['label']}。退款 ¥{amounts['refund_amount']/100:.2f}，保留 ¥{amounts['retained_amount']/100:.2f}，请查看明细；有异议请联系客服。",
            suffix="policy-cancellation")
    return order


@transaction.atomic
def cancel(*, order_no, customer, token, personal_reason_confirmed):
    if not isinstance(token, str) or len(token) > 8192:
        raise ValidationError("费用预览凭证无效，请重新查看。")
    order = get_object_or_404(Order.objects.select_for_update(), order_no=order_no, customer=customer)
    if order.cancellation_record and token and order.cancellation_record.get("request_digest") == hashlib.sha256(token.encode()).hexdigest():
        return order
    if personal_reason_confirmed is not True:
        raise ValidationError("请确认是个人原因取消；其他情况请联系客服核查。")
    ensure_eligible(order)
    try:
        agreed = signing.loads(token, salt=SALT, max_age=300)
    except (signing.BadSignature, TypeError, ValueError):
        raise ValidationError("费用预览已失效，请重新查看后确认。") from None
    now = timezone.now()
    amounts = calculate(order, now=now)
    if agreed != quote_context(order, amounts):
        raise ValidationError("订单阶段或费用已变化，请重新预览并确认。")
    return apply_decision(order, amounts, now=now, token=token, actor=customer)


def confirm_arrival(order, data, photo, *, now):
    if not order.cancellation_policy:
        return
    if order.arrived_at:
        if order.arrival_confirmation.get("photo_id") != str(photo.pk):
            raise ValidationError("首次到场证据已固定。如需更正，请联系客服核实。")
        return
    if not order.departed_at or photo.created_at < max(order.departed_at, now - timedelta(minutes=15)):
        raise ValidationError("请在到场后重新上传当前集合照，不能使用旧照片登记到达。")
    if order.source_longitude is None or order.source_latitude is None:
        raise ValidationError("订单缺少集合地点坐标，请联系客服核实到场。")
    lon, lat, lon2, lat2 = map(math.radians, map(float, (
        data["longitude"], data["latitude"], order.source_longitude, order.source_latitude)))
    a = math.sin((lat-lat2)/2)**2 + math.cos(lat)*math.cos(lat2)*math.sin((lon-lon2)/2)**2
    distance = 6371000 * 2 * math.asin(min(1, math.sqrt(a)))
    if distance > 500 or data.get("accuracy_m") is None or data["accuracy_m"] > 200:
        raise ValidationError("请在集合地点附近重新获取准确定位；定位困难请联系客服核实，不会自动记为到达。")
    order.arrived_at = now
    order.arrival_confirmation = {"photo_id": str(photo.pk), "longitude": str(data["longitude"]),
        "latitude": str(data["latitude"]), "accuracy_m": str(data["accuracy_m"]), "distance_m": round(distance)}


@transaction.atomic
def start_wait(*, order_no, provider_user, confirmed):
    from taskcenter.services import register_provider_customer_wait
    from .timeouts import _notify
    from notifications.models import UserNotification
    order = get_object_or_404(Order.objects.select_for_update(), order_no=order_no, provider__user=provider_user)
    if order.customer_wait.get("state") == "waiting":
        return order
    ensure_eligible(order)
    if confirmed is not True or order.status != Order.Status.DEPARTED or not order.arrived_at or order.customer_wait:
        raise ValidationError("仅首次有效到场、尚未开始服务且确认联系不上用户时可发起等待。")
    if not order.provider_last_contact_at or order.provider_last_contact_at < order.arrived_at:
        raise ValidationError("请在到场后先点击联系用户，确认无法联系后再发起等待。")
    now = timezone.now()
    if order.start_deadline_at and now > order.start_deadline_at:
        raise ValidationError("订单已超过正常开始时间，请联系客服核实，不能补记失联等待。")
    start = max(now, order.starts_at)
    order.customer_wait_deadline_at = start + timedelta(minutes=order.cancellation_policy["config"]["wait_minutes"])
    order.customer_wait = {"state": "waiting", "requested_at": now.isoformat(), "starts_at": start.isoformat(),
        "contact_at": order.provider_last_contact_at.isoformat(), "provider_id": provider_user.pk}
    order.save()
    register_provider_customer_wait(order)
    fee = calculate(order, now=now, no_show=True)["retained_amount"]
    _notify(order, event=UserNotification.EventType.ORDER_AFTER_SALES_STARTED,
        title="达人已到场，请及时联系", suffix="customer-wait",
        content=f"达人报告暂时联系不上你，请在 {timezone.localtime(order.customer_wait_deadline_at):%m月%d日 %H:%M} 前联系。若一直未联系且无争议，订单将取消，预计扣 ¥{fee/100:.2f}。已联系或有异议请在订单中反馈。")
    return order


@transaction.atomic
def respond_wait(*, order_no, actor, role):
    scope = {"customer": actor} if role == "customer" else {"provider__user": actor}
    order = get_object_or_404(Order.objects.select_for_update(), order_no=order_no, **scope)
    if order.customer_wait.get("state") != "waiting" or order.status != Order.Status.DEPARTED:
        raise ValidationError("等待已结束，请刷新查看结果；有异议可联系客服。")
    order.customer_wait = {**order.customer_wait, "state": "responded", "responded_at": timezone.now().isoformat(), "responded_by": role}
    order.save(update_fields=("customer_wait", "updated_at"))
    return order


@transaction.atomic
def expire_wait(order_no, *, now=None):
    from .timeouts import flag_idle_fulfillment
    now = now or timezone.now()
    order = Order.objects.select_for_update().filter(order_no=order_no).first()
    if not order or order.customer_wait.get("state") != "waiting" or order.status != Order.Status.DEPARTED:
        return {"state": "not_applicable"}
    if now < order.customer_wait_deadline_at:
        return {"state": "not_due", "deadline": order.customer_wait_deadline_at}
    try:
        with transaction.atomic():
            ensure_eligible(order)
            apply_decision(order, calculate(order, now=now, no_show=True), now=now)
    except ValidationError:
        order.refresh_from_db()
        order.customer_wait = {**order.customer_wait, "state": "review"}
        flag_idle_fulfillment(order, code="customer_wait_review", label="用户失联取消存在争议或资金待核实",
                              expected=order.customer_wait_deadline_at, now=now)
        order.save()
        return {"state": "review"}
    return {"state": "cancelled"}


def payload(order):
    decision = {k: v for k, v in order.cancellation_record.items() if k not in ("request_digest", "actor_id", "platform_commission_rate")}
    return {"policy": order.cancellation_policy, "transport_mode": order.transport_mode,
            "transport_mode_label": MODES.get(order.transport_mode, ""), "decision": decision,
            "can_preview": bool(order.cancellation_policy and order.status in ELIGIBLE and not order.cancellation_record),
            "arrived_at": order.arrived_at,
            "wait_state": order.customer_wait.get("state", ""), "wait_deadline_at": order.customer_wait_deadline_at,
            "finance_notice": "剩余款须待财务核账，尚未计入可提现余额。" if decision.get("retained_amount") else ""}
