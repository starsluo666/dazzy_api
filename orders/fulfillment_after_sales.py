"""Staff adjudication of fulfillment disputes, not a new payment/refund channel.

Order locks serialize transfer, normal review, adjudication and refund execution.
Signed previews bind facts, rule snapshots, funding and the current case revision.
Only verified refunds change entitlements. Retained money always stays frozen.
"""
import hashlib
import json
from copy import copy
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from backoffice.models import AdminAuditLog, ProviderOrderAfterSalesCase as Case
from backoffice.refund_authorization import authorize_approval, escalate, has, refund_policy
from .models import ProviderOrder as Order, ProviderOrderPaymentOrder as Payment
from .models import ProviderOrderRefundOrder as Refund, ProviderOrderSettlement as Settlement
from .fulfillment import pause_confirmation, suspend_fulfillment_tasks
from .termination import RESPONSIBILITIES, refundable_components, original_commission_rate

SALT = "fulfillment-refund-preview-v1"
OPEN = (Case.Status.PENDING, Case.Status.PROCESSING, Case.Status.APPROVED)
CLOSED_ORDERS = (Order.Status.CANCELLED, Order.Status.REFUNDED, Order.Status.TERMINATED)


def scoped_orders(access):
    qs = Order.objects.all()
    return qs if access.all_data else qs.filter(provider__service_city_code__in=access.city_codes)


def locked_case(access, case_no):
    ref = get_object_or_404(Case, case_no=case_no, order__in=scoped_orders(access))
    order = Order.objects.select_for_update().get(pk=ref.order_id)
    case = Case.objects.select_for_update().get(pk=ref.pk)
    if case.case_type != Case.CaseType.FULFILLMENT:
        raise ValidationError("请按原售后单类型处理，不能重复发起履约退款。")
    case.order = order
    return order, case


def audit(request, access, action, order, details):
    from backoffice.access import client_ip
    AdminAuditLog.objects.create(
        actor=request.user, organization=access.member.organization if access.member else None,
        action=action, target_type="provider_order", target_id=order.order_no,
        after=details, request_id=request.headers.get("X-Request-ID", ""), ip_address=client_ip(request),
    )


def check_funds(order):
    from .distributions import assert_refund_not_distributed
    assert_refund_not_distributed(order)
    if order.cancellation_record or order.departure_timed_out_at or order.status in CLOSED_ORDERS:
        raise ValidationError("订单已取消、终止或退款，请核查原处理记录，不能重复裁定。")
    if Settlement.objects.filter(order=order, status=Settlement.Status.SETTLED).exists():
        raise ValidationError("订单已结算，请转财务核查。")
    payment = Payment.objects.filter(order=order).first()
    if (not payment or payment.status not in (Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED)
            or not order.paid_at or not payment.paid_at or payment.payer_id != order.customer_id
            or payment.payable_amount != order.payable_amount):
        raise ValidationError("订单支付结果或金额尚未核实，不能裁定退款。")
    if order.refund_orders.exclude(status=Refund.Status.SUCCEEDED).exists():
        raise ValidationError("存在未完成或失败待核实的退款，请先核查原退款单。")
    from wallets.models import WalletPaymentAllocation
    allocation = WalletPaymentAllocation.objects.select_for_update().filter(
        business_type=WalletPaymentAllocation.BusinessType.PROVIDER_ORDER, business_order_no=order.order_no,
    ).first()
    if not allocation and payment.channel in (Payment.Channel.BALANCE, Payment.Channel.MIXED):
        raise ValidationError("余额支付分配记录缺失，请转财务核查。")
    if allocation:
        refunds = list(order.refund_orders.all())
        if (allocation.user_id != order.customer_id or allocation.payable_amount != order.payable_amount
                or allocation.wallet_released or allocation.status not in (
                    WalletPaymentAllocation.Status.CONSUMED, WalletPaymentAllocation.Status.PARTIALLY_REFUNDED)
                or allocation.wallet_refunded_amount != sum(r.wallet_refund_amount for r in refunds)
                or allocation.external_refunded_amount != sum(r.external_refund_amount for r in refunds)):
            raise ValidationError("余额扣款或退款记录不一致，请先核账，不能按未核实的金额裁定。")
    from .services import _discounted_order_components
    if sum(_discounted_order_components(order).values()) != order.payable_amount:
        raise ValidationError("订单实付明细不一致，请转财务核查。")


def require_pending(order, case):
    if case.status not in (Case.Status.PENDING, Case.Status.PROCESSING) or order.status != Order.Status.AFTER_SALES:
        raise ValidationError("售后已处理或订单状态发生变化，请刷新查看结果。")
    if not order.fulfillment_review_required:
        raise ValidationError("履约审核状态已变化，请刷新后重新核查。")
    check_funds(order)


@transaction.atomic
def transfer(*, order_no, revision, reason, access, request):
    for permission in ("order.fulfillment.review", "order.after_sales.create", "order.after_sales.view"):
        access.require(permission)
    order = get_object_or_404(scoped_orders(access).select_for_update(), order_no=order_no)
    # Repeat intake opens the original case, never creates a second refund intent.
    existing = order.after_sales_cases.filter(status__in=OPEN).first()
    if existing:
        return existing, False
    if revision != order.fulfillment_revision or not order.fulfillment_review_required:
        raise ValidationError("履约记录或审核状态已变化，请刷新后重新处理。")
    check_funds(order)
    from backoffice.services import create_provider_order_after_sales_case
    case = create_provider_order_after_sales_case(
        order_no=order_no, case_type=Case.CaseType.FULFILLMENT,
        requested_amount=sum(refundable_components(order).values()), reason=reason,
        actor=request.user, access=access, request=request,
    )
    order.refresh_from_db()
    pause_confirmation(order, now=timezone.now())
    suspend_fulfillment_tasks(order)
    order.fulfillment_reviews = [*order.fulfillment_reviews, {
        "revision": revision, "outcome": "after_sales", "reason": reason,
        "case_no": case.case_no, "reviewed_at": timezone.now().isoformat(),
        "reviewer_id": request.user.pk, "reviewer_name": request.user.nickname,
    }]
    order.save()
    case.fulfillment_snapshot = {"version": 1, "revision": revision, "issues": order.fulfillment_issues}
    case.save(update_fields=("fulfillment_snapshot", "updated_at"))
    notify_provider(case, "履约异常已转售后", "平台正在核查本单履约情况，请配合客服处理。", "intake")
    audit(request, access, "order.fulfillment.after_sales", order, {"case_no": case.case_no, "revision": revision})
    return case, True


@transaction.atomic
def investigate(*, order_no, revision, reason, access, request):
    access.require("order.fulfillment.review")
    order = get_object_or_404(scoped_orders(access).select_for_update(), order_no=order_no)
    if (revision != order.fulfillment_revision or not order.fulfillment_review_required
            or order.status in CLOSED_ORDERS):
        raise ValidationError("审核状态已变化，请刷新后重新处理。")
    entry = {"revision": revision, "outcome": "investigating", "reason": reason,
             "reviewer_id": request.user.pk, "reviewer_name": request.user.nickname}
    if order.fulfillment_reviews and all(order.fulfillment_reviews[-1].get(k) == v for k, v in entry.items()):
        return order
    order.fulfillment_reviews = [*order.fulfillment_reviews, {**entry, "reviewed_at": timezone.now().isoformat()}]
    order.save(update_fields=("fulfillment_reviews", "updated_at"))
    audit(request, access, "order.fulfillment.investigate", order, entry)
    return order


@transaction.atomic
def context(*, case_no, access, request):
    from .cancellations import MODES
    access.require("order.after_sales.view")
    order, case = locked_case(access, case_no)
    blocked = ""
    try:
        require_pending(order, case)
    except ValidationError as error:
        blocked = str(error.detail[0] if isinstance(error.detail, list) else error.detail)
    from .services import _discounted_order_components
    paid = _discounted_order_components(order)
    remaining = refundable_components(order)
    return {
        "case_no": case.case_no, "order_no": order.order_no, "revision": order.fulfillment_revision,
        "paid": order.payable_amount, "refunded": order.payable_amount - sum(remaining.values()),
        "components": [{"key": k, "label": label, "paid": paid[k], "remaining": remaining[k]}
                       for k, label in (("service", "服务费"), ("transport", "交通费"), ("other", "其他费用"))],
        "policy_available": bool(order.cancellation_policy and not order.refund_orders.exists()),
        "policy_clauses": order.cancellation_policy.get("clauses", []),
        "transport_mode": MODES.get(order.transport_mode, "未记录"),
        "timeline": {key: getattr(order, key) for key in (
            "paid_at", "starts_at", "ends_at", "departed_at", "arrived_at", "service_started_at", "completion_submitted_at")},
        "blocked_reason": blocked,
        "can_preview": not blocked and all(has(access, p) for p in ("order.after_sales.review", "order.fulfillment.review")),
        "can_approve": has(access, "refund.approve") or has(access, "refund.supervise"),
        "policy": refund_policy(request.user, access),
        "proposed": case.fulfillment_snapshot.get("proposed", {}).get("inputs"),
    }


def calculate_preview(order, case, inputs):
    """Use adjudicated event time, never the later time a staff member clicks approve."""
    from .cancellations import calculate, MODES, DEFAULTS, validate_config
    from .services import _refund_allocation, provider_refund_payment_routes
    require_pending(order, case)
    if inputs["revision"] != order.fulfillment_revision:
        raise ValidationError("订单出现新的履约记录，请刷新并重新核查。")
    event = parse_datetime(inputs["occurred_at"])
    if not event or timezone.is_naive(event) or not order.paid_at <= event <= timezone.now():
        raise ValidationError("核实的取消或结束时间必须在付款之后、当前时间之前，并包含时区。")
    facts = copy(order)
    for key in ("departed_at", "arrived_at", "service_started_at"):
        value = getattr(order, key)
        setattr(facts, key, value if value and value <= event else None)
    stage = "in_service" if facts.service_started_at else "arrived" if facts.arrived_at else "departed" if facts.departed_at else "before_departure"
    components = refundable_components(order)
    if inputs["mode"] == "policy":
        if inputs["responsibility"] != "customer":
            raise ValidationError("用户取消扣费规则仅适用于核实为用户责任的订单。")
        if not order.cancellation_policy or order.transport_mode not in MODES:
            raise ValidationError("本单未保存适用的取消规则，请按证据人工裁定，不使用当前新规则补扣。")
        if order.refund_orders.exists():
            raise ValidationError("本单已有退款，不能再次完整套用取消规则，请人工核定剩余金额。")
        if not isinstance(order.cancellation_policy.get("config"), dict) or set(order.cancellation_policy["config"]) != set(DEFAULTS):
            raise ValidationError("本单取消规则快照不完整，请人工核查，不能补用新规则。")
        facts.cancellation_policy = {**order.cancellation_policy, "config": validate_config(order.cancellation_policy["config"])}
        no_show = inputs["customer_no_show"]
        if no_show:
            wait = order.customer_wait
            contact = parse_datetime(wait.get("contact_at", ""))
            start = parse_datetime(wait.get("starts_at", ""))
            if (stage != "arrived" or not order.customer_wait_deadline_at or event < order.customer_wait_deadline_at
                    or not contact or timezone.is_naive(contact) or contact < facts.arrived_at or contact > event
                    or not start or timezone.is_naive(start) or start < order.starts_at or start < facts.arrived_at or start > event
                    or event < start + timedelta(minutes=facts.cancellation_policy["config"]["wait_minutes"])
                    or wait.get("responded_at") or wait.get("state") not in ("waiting", "review")):
                raise ValidationError("缺少有效到场、联系或满时等待记录，或用户已回应，不能按失联规则扣费。")
        policy_quote = calculate(facts, now=event, no_show=no_show)
        refunds = policy_quote["component_refunds"]
        label = policy_quote["label"]
        basis = policy_quote["compensation_amount"] + policy_quote["retained_service_amount"]
        travel = policy_quote["retained_travel_amount"]
        rule = policy_quote["rule"]
    else:
        if inputs["customer_no_show"]:
            raise ValidationError("人工裁定不直接套用用户失联扣费规则。")
        refunds = inputs.get("component_refunds")
        if not isinstance(refunds, dict) or set(refunds) != set(components):
            raise ValidationError("请分别填写服务费、交通费及其他费用的退款金额。")
        label, rule = "客服按履约证据人工裁定", "manual"
        basis = components["service"] - refunds["service"]
        travel = components["transport"] - refunds["transport"]
    if any(type(v) is not int or v < 0 or v > components[k] for k, v in refunds.items()):
        raise ValidationError("各项退款必须在该项剩余可退金额内。")
    amount = sum(refunds.values())
    if amount > case.requested_amount:
        raise ValidationError("本次退款超过登记时的可退上限。")
    if amount:
        _refund_allocation(order, amount, components_override=refunds)
    rate = original_commission_rate(order, refund_amount=amount)
    remaining = sum(components.values()) - amount
    platform = int((Decimal(basis) * rate / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return {
        "rule": rule, "label": label, "stage": stage,
        "policy_version": order.cancellation_policy.get("version", "") if inputs["mode"] == "policy" else "",
        "responsibility": inputs["responsibility"], "responsibility_label": RESPONSIBILITIES[inputs["responsibility"]],
        "occurred_at": inputs["occurred_at"], "component_refunds": refunds,
        "refund_amount": amount, "remaining_amount": remaining,
        "refunded_before": order.payable_amount - sum(components.values()),
        "routes": provider_refund_payment_routes(order, amount),
        "retained_service_amount": basis, "retained_transport_amount": travel,
        "retained_other_amount": remaining - basis - travel,
        "platform_commission_rate": str(rate), "platform_amount": platform, "provider_amount": remaining - platform,
        "terminal_status": Order.Status.TERMINATED if facts.service_started_at else Order.Status.CANCELLED,
        "requires_supervisor": inputs["mode"] == "manual" and inputs["responsibility"] == "customer",
    }


def fingerprint(order, case):
    state = {"order_updated": str(order.updated_at), "case_updated": str(case.updated_at),
             "revision": order.fulfillment_revision, "policy": order.cancellation_policy,
             "refunds": list(order.refund_orders.order_by("pk").values("pk", "status", "refund_amount"))}
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


@transaction.atomic
def preview(*, case_no, inputs, access, request):
    access.require("order.after_sales.review")
    access.require("order.fulfillment.review")
    order, case = locked_case(access, case_no)
    quote = calculate_preview(order, case, inputs)
    token = signing.dumps({"case_no": case_no, "inputs": inputs, "quote": quote,
                           "fingerprint": fingerprint(order, case)}, salt=SALT, compress=True)
    return {**quote, "token": token}


@transaction.atomic
def resolve(*, case_no, token, access, request):
    access.require("order.after_sales.review")
    access.require("order.fulfillment.review")
    from backoffice.refund_authorization import require_approval
    require_approval(access)
    try:
        payload = signing.loads(token, salt=SALT, max_age=900)
    except signing.BadSignature:
        raise ValidationError("退款预览已过期或无效，请重新预览。") from None
    order, case = locked_case(access, case_no)
    digest = hashlib.sha256(token.encode()).hexdigest()
    if case.fulfillment_snapshot.get("decision", {}).get("request_digest") == digest:
        return case  # Exact retry returns the original outcome, even after refund completion.
    if payload.get("case_no") != case_no or payload.get("fingerprint") != fingerprint(order, case):
        raise ValidationError("订单或售后记录已变化，请刷新并重新预览，未发起退款。")
    inputs = payload["inputs"]
    quote = calculate_preview(order, case, inputs)
    if quote != payload["quote"]:
        raise ValidationError("退款金额或支付路径已变化，请重新预览。")
    case.fulfillment_snapshot = {**case.fulfillment_snapshot, "proposed": {"inputs": inputs, "quote": quote}}
    case.save(update_fields=("fulfillment_snapshot", "updated_at"))
    if quote["requires_supervisor"] and not has(access, "refund.supervise"):
        return escalate(case, reason="用户责任未按原取消规则处理，需主管核实人工裁定依据。",
                        actor=request.user, access=access, request=request)
    if not authorize_approval(case, amount=quote["refund_amount"], refunds=order.refund_orders.all(),
                              actor=request.user, access=access, request=request, result_note=inputs["result_note"]):
        return case
    now = timezone.now()
    decision = {**quote, "inputs": inputs, "request_digest": digest, "decided_at": now.isoformat()}
    case.fulfillment_snapshot = {**case.fulfillment_snapshot, "decision": decision}
    case.status = Case.Status.APPROVED if quote["refund_amount"] else Case.Status.RESOLVED
    case.approved_amount = quote["refund_amount"]
    case.result_note = inputs["result_note"]
    case.reviewed_by, case.reviewed_at = request.user, now
    case.save()
    order.status = quote["terminal_status"]
    order.cancelled_at = now
    order.confirmation_expires_at = None
    order.confirmation_remaining_seconds = None
    order.fulfillment_review_required = False
    order.fulfillment_issues = [i if i.get("resolved_at") else {
        **i, "resolved_at": now.isoformat(), "outcome": "after_sales_decided", "case_no": case_no,
    } for i in order.fulfillment_issues]
    order.fulfillment_reviews = [*order.fulfillment_reviews, {
        "revision": order.fulfillment_revision, "outcome": "after_sales_decided", "case_no": case_no,
        "reason": case.result_note, "reviewed_at": now.isoformat(),
        "reviewer_id": request.user.pk, "reviewer_name": request.user.nickname,
    }]
    if order.customer_wait:
        order.customer_wait = {**order.customer_wait, "state": "closed", "closed_at": now.isoformat()}
    order.save()
    suspend_fulfillment_tasks(order)
    if quote["refund_amount"]:
        from .services import create_provider_order_refund
        create_provider_order_refund(order_no=order.order_no, amount=quote["refund_amount"],
            source_type=Refund.SourceType.AFTER_SALES, source_reference=case_no,
            idempotency_key=f"provider-after-sales:{case_no}", reason=case.result_note,
            operator=request.user, components_override=quote["component_refunds"])
    else:
        reconcile(order, case)
    from .settlement_plans import sync_provider_settlement_plan
    sync_provider_settlement_plan(order_no=order.order_no)
    audit(request, access, "order.fulfillment.adjudicate", order, {"case_no": case_no, "decision": decision})
    from notifications.models import UserNotification
    from notifications.services import create_order_notification
    content = "客服已完成本单核查，请查看处理结论及退款进度。"
    create_order_notification(order=order, event_type=UserNotification.EventType.ORDER_AFTER_SALES_RESULT,
        title="履约售后已处理", content=content, dedupe_suffix=f"{case_no}:fulfillment")
    notify_provider(case, "履约售后已处理", content, "decision")
    return case


def reconcile(order, case):
    """Caller holds order lock. No disbursement and no fabricated refund success."""
    decision = case.fulfillment_snapshot.get("decision")
    if not decision or order.refund_orders.exclude(status=Refund.Status.SUCCEEDED).exists():
        return
    refunded = sum(r.refund_amount for r in order.refund_orders.all())
    if refunded != decision["refunded_before"] + decision["refund_amount"]:
        return
    retained = decision["remaining_amount"]
    now = timezone.now()
    Settlement.objects.update_or_create(order=order, defaults={
        "provider": order.provider, "paid_amount": order.payable_amount, "refunded_amount": refunded,
        "net_service_fee_amount": decision["retained_service_amount"],
        "net_transport_fee_amount": decision["retained_transport_amount"],
        "net_other_fee_amount": decision["retained_other_amount"],
        "platform_commission_rate": Decimal(decision["platform_commission_rate"]),
        "platform_commission_amount": decision["platform_amount"],
        "provider_service_income_amount": decision["retained_service_amount"] - decision["platform_amount"],
        "provider_settlement_amount": decision["provider_amount"],
        "calculation_snapshot": {"version": SALT, "case_no": case.case_no, "decision": decision},
        "frozen_at": now, "freeze_until": now,
        "status": Settlement.Status.DISPUTE_FROZEN if retained else Settlement.Status.CANCELLED,
        "dispute_reason": "履约售后剩余款待财务核账，暂不自动分账" if retained else "",
        "cancelled_at": None if retained else now,
    })
    from .settlement_plans import sync_provider_settlement_plan
    sync_provider_settlement_plan(order_no=order.order_no)


def notify_provider(case, title, content, suffix):
    from notifications.models import UserNotification
    from notifications.services import create_notification
    create_notification(recipient=case.order.provider.user, category=UserNotification.Category.ORDER,
        event_type=UserNotification.EventType.ORDER_AFTER_SALES_RESULT if suffix == "decision" else UserNotification.EventType.ORDER_AFTER_SALES_STARTED,
        title=title, content=content, target_type="provider_order", target_id=case.order.order_no,
        action_text="查看订单", action_url=f"/pages/orders/detail?order_no={case.order.order_no}",
        dedupe_key=f"fulfillment-case:{case.case_no}:{suffix}:provider")


def case_payload(case, *, internal=False):
    if case.case_type != Case.CaseType.FULFILLMENT:
        return None
    decision = case.fulfillment_snapshot.get("decision")
    state, label = "review_pending", "待客服核实责任及处理方案"
    if case.status == Case.Status.REJECTED:
        state, label = "rejected", "售后已驳回，履约异常仍待核实"
    elif decision:
        state, label = ("refund_pending", "退款处理中") if case.status == Case.Status.APPROVED else (
            ("reconciliation_pending", "剩余款待财务核账") if decision["remaining_amount"] else ("closed", "已处理，无剩余款")
        )
        if case.status == Case.Status.APPROVED and case.order.refund_orders.filter(
            source_reference=case.case_no, status=Refund.Status.FAILED,
        ).exists():
            state, label = "refund_attention", "退款异常，待核实原退款单"
    keys = ("responsibility_label", "occurred_at", "label", "component_refunds", "refund_amount", "remaining_amount")
    return {"revision": case.fulfillment_snapshot.get("revision"), "finance_state": state, "finance_label": label,
            "decision": {k: decision[k] for k in keys} if decision else None,
            **({"issues": case.fulfillment_snapshot.get("issues", [])} if internal else {})}
