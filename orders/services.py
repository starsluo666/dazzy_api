import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Avg, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from config.payment_capabilities import ensure_provider_order_payment_scene_available
from notifications.models import UserNotification
from notifications.services import (
    create_notification,
    create_order_notification,
    create_provider_new_order_notification,
)

from providers.models import ProviderProfile, ProviderService

from .models import (
    HuifuPaymentNotification,
    HuifuRefundNotification,
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderRefundOrder,
    ProviderOrderReview,
    ProviderOrderSettlement,
)
from .huifu import (
    HuifuGatewayError,
    HuifuPaymentQueryResult,
    HuifuRefundQueryResult,
    HuifuPaymentSessionInProgress,
    HuifuPaymentSessionResult,
    HuifuRefundTerminalError,
    get_huifu_payment_gateway,
)

MINIMUM_ADVANCE = timedelta(hours=1)
MAXIMUM_ADVANCE = timedelta(days=3)
MINIMUM_HOURLY_MINUTES = 120
TIME_GRAIN_MINUTES = 30
PAYMENT_LOCK_MINUTES = 15
PROVIDER_REJECTION_SUPPORT_TIMEOUT = timedelta(minutes=15)
PAYMENT_SESSION_STALE_AFTER = timedelta(seconds=30)
HUIFU_CLOSE_MINIMUM_AGE = timedelta(minutes=1)

PAYMENT_SCENE_TRADE_TYPES = {
    "official_account": "T_JSAPI",
    "mobile_app": "T_APP",
}


def provider_rejection_refund_reference(order_no: str) -> str:
    return f"provider-rejection-timeout:{order_no}"


def create_provider_order_payment_order(order: ProviderOrder):
    return ProviderOrderPaymentOrder.objects.get_or_create(
        order=order,
        defaults={
            "payer": order.customer,
            "service_fee_amount": order.service_fee_amount,
            "transport_fee_amount": order.transport_fee_amount,
            "other_fee_amount": order.other_fee_amount,
            "discount_amount": order.discount_amount,
            "payable_amount": order.payable_amount,
            "pricing_snapshot": order.pricing_snapshot,
            "expires_at": order.payment_expires_at,
        },
    )


def _stored_huifu_payment_session(
    payment: ProviderOrderPaymentOrder,
) -> HuifuPaymentSessionResult:
    return HuifuPaymentSessionResult(
        req_seq_id=payment.req_seq_id,
        req_date=payment.req_date,
        huifu_id=payment.gateway_merchant_id,
        trade_type=payment.trade_type,
        trans_stat="P",
        hf_seq_id=payment.gateway_trade_no,
        party_order_id=payment.gateway_party_order_id,
        out_trans_id=payment.gateway_out_trans_id,
        pay_info=payment.payment_invoke_payload,
        response_code=payment.gateway_response_code,
        response_digest=payment.gateway_response_digest,
    )


def create_provider_order_huifu_payment_session(
    *,
    order_id: int,
    customer_id: int,
    payment_scene: str,
    sub_openid: str = "",
):
    """Create or replay one idempotent Huifu aggregate payment session."""

    ensure_provider_order_payment_scene_available(payment_scene)
    gateway = get_huifu_payment_gateway()
    try:
        trade_type = PAYMENT_SCENE_TRADE_TYPES[payment_scene]
    except KeyError as exc:
        raise ValidationError({"payment_scene": "当前支付场景尚未开放。"}) from exc
    gateway.validate_for_payment(trade_type=trade_type)
    if trade_type == "T_JSAPI" and not sub_openid:
        raise ValidationError({"authorization": "请先完成微信服务号网页授权。"})
    now = timezone.now()
    with transaction.atomic():
        order = ProviderOrder.objects.select_for_update().get(
            pk=order_id,
            customer_id=customer_id,
        )
        payment, _ = create_provider_order_payment_order(order)
        payment = ProviderOrderPaymentOrder.objects.select_for_update().get(pk=payment.pk)
        if order.status != ProviderOrder.Status.PENDING_PAYMENT:
            raise ValidationError({"status": "订单不在待支付状态。"})
        if payment.status != ProviderOrderPaymentOrder.Status.PENDING_PAYMENT:
            raise ValidationError({"status": "支付单不在待支付状态。"})
        if order.payment_expires_at <= now:
            raise ValidationError({"status": "支付已超时，请重新下单。"})
        if (
            payment.preorder_status == ProviderOrderPaymentOrder.PreorderStatus.READY
            and payment.payment_scene == payment_scene
            and payment.trade_type == trade_type
            and payment.payment_invoke_payload
        ):
            return _stored_huifu_payment_session(payment), False
        if (
            payment.preorder_status == ProviderOrderPaymentOrder.PreorderStatus.SUBMITTING
            and payment.preorder_requested_at
            and payment.preorder_requested_at > now - PAYMENT_SESSION_STALE_AFTER
        ):
            raise HuifuPaymentSessionInProgress()
        if payment.payment_scene and payment.payment_scene != payment_scene:
            raise ValidationError({"payment_scene": "当前支付单已绑定其他支付场景。"})

        payment.req_date = payment.req_date or timezone.localtime(now).strftime("%Y%m%d")
        payment.req_seq_id = payment.req_seq_id or payment.payment_no
        payment.gateway_merchant_id = gateway.merchant_id
        payment.payment_scene = payment_scene
        payment.trade_type = trade_type
        payment.preorder_status = ProviderOrderPaymentOrder.PreorderStatus.SUBMITTING
        payment.preorder_requested_at = now
        payment.preorder_attempts += 1
        payment.gateway_response_code = ""
        payment.payment_invoke_payload = {}
        payment.save(
            update_fields=(
                "req_date",
                "req_seq_id",
                "gateway_merchant_id",
                "payment_scene",
                "trade_type",
                "preorder_status",
                "preorder_requested_at",
                "preorder_attempts",
                "gateway_response_code",
                "payment_invoke_payload",
                "updated_at",
            )
        )
        req_date = payment.req_date
        req_seq_id = payment.req_seq_id
        amount = payment.payable_amount
        goods_desc = order.service_name_snapshot
        attach = order.order_no
        time_expire = timezone.localtime(order.payment_expires_at).strftime("%Y%m%d%H%M%S")

    try:
        result = gateway.create_payment(
            req_date=req_date,
            req_seq_id=req_seq_id,
            amount=amount,
            goods_desc=goods_desc,
            trade_type=trade_type,
            attach=attach,
            time_expire=time_expire,
            sub_openid=sub_openid,
        )
    except HuifuGatewayError as exc:
        with transaction.atomic():
            payment = ProviderOrderPaymentOrder.objects.select_for_update().get(order_id=order_id)
            if payment.req_date == req_date and payment.req_seq_id == req_seq_id:
                payment.preorder_status = ProviderOrderPaymentOrder.PreorderStatus.FAILED
                payment.gateway_response_code = exc.response_code[:32]
                payment.gateway_response_digest = exc.response_digest[:64]
                payment.save(
                    update_fields=(
                        "preorder_status",
                        "gateway_response_code",
                        "gateway_response_digest",
                        "updated_at",
                    )
                )
        raise

    with transaction.atomic():
        payment = ProviderOrderPaymentOrder.objects.select_for_update().get(order_id=order_id)
        if payment.req_date != req_date or payment.req_seq_id != req_seq_id:
            raise HuifuGatewayError("支付请求流水已变更，请重新进入支付页。")
        payment.preorder_status = ProviderOrderPaymentOrder.PreorderStatus.READY
        payment.gateway_merchant_id = result.huifu_id
        payment.gateway_trade_no = result.hf_seq_id
        payment.gateway_party_order_id = result.party_order_id
        payment.gateway_out_trans_id = result.out_trans_id
        payment.payment_invoke_payload = result.pay_info
        payment.gateway_response_code = result.response_code
        payment.gateway_response_digest = result.response_digest
        payment.preorder_ready_at = timezone.now()
        payment.save(
            update_fields=(
                "preorder_status",
                "gateway_merchant_id",
                "gateway_trade_no",
                "gateway_party_order_id",
                "gateway_out_trans_id",
                "payment_invoke_payload",
                "gateway_response_code",
                "gateway_response_digest",
                "preorder_ready_at",
                "updated_at",
            )
        )
        return _stored_huifu_payment_session(payment), True


def _huifu_amount_to_cents(value: str) -> int:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HuifuGatewayError("支付通道返回的金额格式无效。") from exc
    cents = amount * Decimal("100")
    if cents != cents.to_integral_value() or cents < 0:
        raise HuifuGatewayError("支付通道返回的金额格式无效。")
    return int(cents)


def _huifu_payment_channel(pay_type: str) -> str:
    if pay_type.startswith("T_"):
        return ProviderOrderPaymentOrder.Channel.WECHAT
    if pay_type.startswith("A_"):
        return ProviderOrderPaymentOrder.Channel.ALIPAY
    raise HuifuGatewayError("支付通道返回了当前未开放的支付方式。")


def _huifu_paid_at(value: str):
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
    except (TypeError, ValueError) as exc:
        raise HuifuGatewayError("支付通道未返回有效的交易完成时间。") from exc
    return timezone.make_aware(parsed, timezone.get_current_timezone())


def _query_provider_order_huifu_payment(
    payment: ProviderOrderPaymentOrder,
    *,
    fallback_hf_seq_id: str = "",
) -> HuifuPaymentQueryResult:
    gateway = get_huifu_payment_gateway()
    query = gateway.query_payment(
        req_date=payment.req_date,
        req_seq_id=payment.req_seq_id,
        hf_seq_id=payment.gateway_trade_no or fallback_hf_seq_id,
    )
    if query.huifu_id != payment.gateway_merchant_id:
        raise HuifuGatewayError("支付查询返回的商户号与本地支付单不一致。")
    if query.trans_amt:
        if _huifu_amount_to_cents(query.trans_amt) != payment.payable_amount:
            raise HuifuGatewayError("支付查询金额与本地支付单不一致。")
    elif query.trans_stat == "S":
        raise HuifuGatewayError("支付成功查询未返回交易金额。")
    if query.trade_type and payment.trade_type and query.trade_type != payment.trade_type:
        raise HuifuGatewayError("支付查询返回的交易类型与本地支付单不一致。")
    queried_at = timezone.now()
    ProviderOrderPaymentOrder.objects.filter(pk=payment.pk).update(
        gateway_last_query_status=query.trans_stat,
        gateway_last_query_digest=query.response_digest,
        gateway_last_queried_at=queried_at,
        updated_at=queried_at,
    )
    return query


def _apply_huifu_payment_query_success(
    payment: ProviderOrderPaymentOrder,
    query: HuifuPaymentQueryResult,
    *,
    fallback_hf_seq_id: str = "",
    fallback_end_time: str = "",
):
    if query.trans_stat != "S":
        return None
    gateway_trade_no = query.gateway_trade_no or fallback_hf_seq_id
    if not gateway_trade_no:
        raise HuifuGatewayError("支付查询未返回汇付全局流水号。")
    channel_trade_type = query.trade_type or payment.trade_type
    paid_at = _huifu_paid_at(query.end_time or fallback_end_time)
    if (
        payment.order.status in (ProviderOrder.Status.CANCELLED, ProviderOrder.Status.REFUNDED)
        or (
            payment.order.status == ProviderOrder.Status.PENDING_PAYMENT
            and payment.order.payment_expires_at <= paid_at
        )
    ):
        return _apply_cancelled_huifu_payment_success(
            payment=payment,
            gateway_trade_no=gateway_trade_no,
            trade_type=channel_trade_type,
            paid_at=paid_at,
        )
    result = apply_provider_order_payment_success(
        order_no=payment.order.order_no,
        customer_id=payment.payer_id,
        channel=_huifu_payment_channel(channel_trade_type),
        gateway_trade_no=gateway_trade_no,
        paid_amount=payment.payable_amount,
        signature_verified=True,
        now=paid_at,
    )
    if result[1].status not in (
        ProviderOrderPaymentOrder.Status.PAID,
        ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
        ProviderOrderPaymentOrder.Status.REFUNDED,
    ):
        raise HuifuGatewayError("支付成功时间晚于订单失效时间，需要人工核对退款。")
    return result


def _apply_cancelled_huifu_payment_success(
    *,
    payment: ProviderOrderPaymentOrder,
    gateway_trade_no: str,
    trade_type: str,
    paid_at,
):
    """Record financial truth for a cancelled order and reserve a full refund."""

    from taskcenter.services import cancel_provider_order_payment_expiry

    with transaction.atomic():
        order = ProviderOrder.objects.select_for_update().get(pk=payment.order_id)
        locked_payment = ProviderOrderPaymentOrder.objects.select_for_update().get(
            pk=payment.pk
        )
        if order.status == ProviderOrder.Status.PENDING_PAYMENT:
            order.status = ProviderOrder.Status.CANCELLED
            order.cancelled_at = timezone.now()
        elif order.status not in (
            ProviderOrder.Status.CANCELLED,
            ProviderOrder.Status.REFUNDED,
        ):
            raise HuifuGatewayError("已取消支付的订单状态发生变化，需要人工核对。")

        changed = locked_payment.status not in (
            ProviderOrderPaymentOrder.Status.PAID,
            ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
            ProviderOrderPaymentOrder.Status.REFUNDED,
        )
        if locked_payment.status != ProviderOrderPaymentOrder.Status.REFUNDED:
            locked_payment.channel = _huifu_payment_channel(trade_type)
            locked_payment.status = ProviderOrderPaymentOrder.Status.PAID
            locked_payment.gateway_trade_no = gateway_trade_no
            locked_payment.paid_at = paid_at
            locked_payment.closed_at = None
            locked_payment.save(
                update_fields=(
                    "channel",
                    "status",
                    "gateway_trade_no",
                    "paid_at",
                    "closed_at",
                    "updated_at",
                )
            )
        if not order.paid_at:
            order.paid_at = paid_at
        order.save(
            update_fields=("status", "cancelled_at", "paid_at", "updated_at")
        )
        cancel_provider_order_payment_expiry(order.order_no, "cancel_compensation_paid")

    if locked_payment.status != ProviderOrderPaymentOrder.Status.REFUNDED:
        create_provider_order_refund(
            order_no=order.order_no,
            amount=locked_payment.payable_amount,
            source_type=ProviderOrderRefundOrder.SourceType.SYSTEM,
            source_reference=f"cancel-compensation:{order.order_no}",
            idempotency_key=f"provider-order-cancel-compensation:{order.order_no}",
            reason="订单取消后支付成功，系统自动原路退款",
        )
    return order, locked_payment, changed


def confirm_provider_order_huifu_payment_status(
    *,
    order_no: str,
    customer_id: int | None = None,
) -> dict:
    """Actively query Huifu so a missing notification cannot strand a paid order."""

    queryset = ProviderOrder.objects.select_related("payment_order")
    if customer_id is not None:
        queryset = queryset.filter(customer_id=customer_id)
    order = queryset.filter(order_no=order_no).first()
    if order is None:
        raise ValidationError({"order": "订单不存在或无权操作。"})
    payment = getattr(order, "payment_order", None)
    if payment is None:
        return {"state": "not_started", "order_no": order_no}
    if payment.status in (
        ProviderOrderPaymentOrder.Status.PAID,
        ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
        ProviderOrderPaymentOrder.Status.REFUNDED,
    ):
        return {"state": "paid", "order_no": order_no, "changed": False}
    if order.status != ProviderOrder.Status.PENDING_PAYMENT:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    if not payment.req_date or not payment.req_seq_id or not payment.gateway_merchant_id:
        return {"state": "not_started", "order_no": order_no}

    query = _query_provider_order_huifu_payment(payment)
    applied = _apply_huifu_payment_query_success(payment, query)
    if applied is not None:
        return {
            "state": "paid",
            "order_no": order_no,
            "changed": applied[2],
            "trans_stat": query.trans_stat,
        }
    return {
        "state": "failed" if query.trans_stat == "F" else "processing",
        "order_no": order_no,
        "trans_stat": query.trans_stat,
    }


@transaction.atomic
def cancel_provider_order_with_compensation(*, order_no: str, customer_id: int):
    """Cancel locally and keep a durable task for any in-flight real payment."""

    from taskcenter.services import (
        cancel_provider_order_payment_expiry,
        register_provider_order_cancel_compensation,
    )

    order = ProviderOrder.objects.select_for_update().get(
        order_no=order_no,
        customer_id=customer_id,
    )
    if order.status != ProviderOrder.Status.PENDING_PAYMENT:
        raise ValidationError({"status": "当前阶段暂不支持用户直接取消，请联系客服。"})
    now = timezone.now()
    order.status = ProviderOrder.Status.CANCELLED
    order.cancelled_at = now
    order.save(update_fields=("status", "cancelled_at", "updated_at"))
    payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
    has_real_payment_request = bool(
        payment
        and payment.gateway_merchant_id
        and payment.req_date
        and payment.req_seq_id
    )
    if payment and payment.status == ProviderOrderPaymentOrder.Status.PENDING_PAYMENT:
        if has_real_payment_request:
            register_provider_order_cancel_compensation(order)
        else:
            payment.status = ProviderOrderPaymentOrder.Status.CLOSED
            payment.closed_at = now
            payment.save(update_fields=("status", "closed_at", "updated_at"))
    cancel_provider_order_payment_expiry(order_no, "customer_cancelled")
    return order


def _cancel_compensation_refund_state(order_no: str) -> dict | None:
    refund = ProviderOrderRefundOrder.objects.filter(
        idempotency_key=f"provider-order-cancel-compensation:{order_no}"
    ).first()
    if refund is None:
        return None
    state = {
        ProviderOrderRefundOrder.Status.SUCCEEDED: "refunded",
        ProviderOrderRefundOrder.Status.FAILED: "refund_failed",
    }.get(refund.status, "refund_processing")
    return {
        "state": state,
        "order_no": order_no,
        "refund_no": refund.refund_no,
        "refund_status": refund.status,
    }


def _mark_cancelled_payment_closed(payment_id: int, *, now):
    with transaction.atomic():
        payment = ProviderOrderPaymentOrder.objects.select_for_update().get(pk=payment_id)
        if payment.status == ProviderOrderPaymentOrder.Status.PENDING_PAYMENT:
            payment.status = ProviderOrderPaymentOrder.Status.CLOSED
            payment.closed_at = now
            payment.save(update_fields=("status", "closed_at", "updated_at"))


def _save_huifu_close_result(payment_id: int, result, *, queried: bool):
    values = {
        "gateway_close_status": result.trans_stat,
        "gateway_close_response_code": result.response_code,
        "gateway_close_response_digest": result.response_digest,
        "updated_at": timezone.now(),
    }
    if queried:
        values["gateway_close_queried_at"] = timezone.now()
    ProviderOrderPaymentOrder.objects.filter(pk=payment_id).update(**values)


def process_provider_order_cancel_compensation(order_no: str, *, now=None) -> dict:
    """Query first, then close an unpaid trade or refund a late successful trade."""

    now = now or timezone.now()
    order = ProviderOrder.objects.select_related("payment_order").filter(
        order_no=order_no
    ).first()
    if order is None:
        return {"state": "missing", "order_no": order_no}
    payment = getattr(order, "payment_order", None)
    if payment is None:
        return {"state": "not_applicable", "order_no": order_no}
    if order.status == ProviderOrder.Status.REFUNDED:
        return {"state": "refunded", "order_no": order_no}
    if order.status != ProviderOrder.Status.CANCELLED:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    refund_state = _cancel_compensation_refund_state(order_no)
    if refund_state is not None:
        return refund_state
    if payment.status in (
        ProviderOrderPaymentOrder.Status.PAID,
        ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
    ):
        create_provider_order_refund(
            order_no=order_no,
            amount=payment.payable_amount,
            source_type=ProviderOrderRefundOrder.SourceType.SYSTEM,
            source_reference=f"cancel-compensation:{order_no}",
            idempotency_key=f"provider-order-cancel-compensation:{order_no}",
            reason="订单取消后支付成功，系统自动原路退款",
        )
        return _cancel_compensation_refund_state(order_no)
    if payment.status == ProviderOrderPaymentOrder.Status.CLOSED:
        return {"state": "closed", "order_no": order_no}
    if not payment.gateway_merchant_id or not payment.req_date or not payment.req_seq_id:
        _mark_cancelled_payment_closed(payment.pk, now=now)
        return {"state": "closed", "order_no": order_no, "source": "local"}

    query = _query_provider_order_huifu_payment(payment)
    if query.trans_stat == "S":
        _apply_huifu_payment_query_success(payment, query)
        return _cancel_compensation_refund_state(order_no) or {
            "state": "refund_processing",
            "order_no": order_no,
        }
    if query.trans_stat == "F":
        _mark_cancelled_payment_closed(payment.pk, now=now)
        return {"state": "closed", "order_no": order_no, "source": "payment_query"}

    close_eligible_at = (payment.preorder_requested_at or payment.created_at) + (
        HUIFU_CLOSE_MINIMUM_AGE
    )
    if now < close_eligible_at:
        return {
            "state": "not_due",
            "order_no": order_no,
            "deadline": close_eligible_at,
        }

    gateway = get_huifu_payment_gateway()
    with transaction.atomic():
        locked_payment = ProviderOrderPaymentOrder.objects.select_for_update().get(
            pk=payment.pk
        )
        request_date = timezone.localtime(now).strftime("%Y%m%d")
        if not locked_payment.close_req_seq_id:
            locked_payment.close_req_date = request_date
            locked_payment.close_req_seq_id = f"{locked_payment.payment_no}CLOSE"
        if not locked_payment.close_query_req_seq_id:
            locked_payment.close_query_req_date = request_date
            locked_payment.close_query_req_seq_id = f"{locked_payment.payment_no}CLOSEQ"
        locked_payment.save(
            update_fields=(
                "close_req_date",
                "close_req_seq_id",
                "close_query_req_date",
                "close_query_req_seq_id",
                "updated_at",
            )
        )
        close_kwargs = {
            "org_req_date": locked_payment.req_date,
            "org_req_seq_id": locked_payment.req_seq_id,
            "org_hf_seq_id": locked_payment.gateway_trade_no,
        }
        query_existing_close = bool(locked_payment.gateway_close_status)
        if query_existing_close:
            close_req_date = locked_payment.close_query_req_date
            close_req_seq_id = locked_payment.close_query_req_seq_id
        else:
            close_req_date = locked_payment.close_req_date
            close_req_seq_id = locked_payment.close_req_seq_id

    if query_existing_close:
        close_result = gateway.query_close(
            req_date=close_req_date,
            req_seq_id=close_req_seq_id,
            **close_kwargs,
        )
    else:
        close_result = gateway.close_payment(
            req_date=close_req_date,
            req_seq_id=close_req_seq_id,
            **close_kwargs,
        )
    _save_huifu_close_result(
        payment.pk,
        close_result,
        queried=query_existing_close,
    )
    if close_result.trans_stat == "S" or (
        close_result.trans_stat == "F" and close_result.org_trans_stat == "F"
    ):
        _mark_cancelled_payment_closed(payment.pk, now=now)
        return {"state": "closed", "order_no": order_no, "source": "gateway_close"}
    return {
        "state": "processing",
        "order_no": order_no,
        "trans_stat": close_result.trans_stat,
        "org_trans_stat": close_result.org_trans_stat,
    }


def process_huifu_payment_notification(*, resp_data: str, sign: str) -> str:
    """Verify and route aggregate payment/refund notifications."""

    gateway = get_huifu_payment_gateway()
    if not resp_data or not sign:
        raise ValidationError({"notification": "支付通知缺少 resp_data 或 sign。"})
    if not gateway.verify_payment_notification(resp_data=resp_data, sign=sign):
        raise ValidationError({"signature": "支付通知验签失败。"})
    try:
        payload = json.loads(resp_data)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"notification": "支付通知业务数据不是有效 JSON。"}) from exc
    if not isinstance(payload, dict):
        raise ValidationError({"notification": "支付通知业务数据格式无效。"})

    fields = {
        name: str(payload.get(name, ""))
        for name in (
            "huifu_id",
            "req_date",
            "req_seq_id",
            "hf_seq_id",
            "trans_stat",
            "trans_amt",
            "ord_amt",
            "trans_type",
            "notify_type",
            "end_time",
        )
    }
    if any(not fields[name] for name in ("huifu_id", "req_date", "req_seq_id")):
        raise ValidationError({"notification": "支付通知缺少订单识别字段。"})
    if fields["trans_stat"] not in {"", "P", "S", "F", "I"}:
        raise ValidationError({"notification": "支付通知交易状态无效。"})

    payload_digest = sha256(resp_data.encode("utf-8")).hexdigest()
    if fields["trans_type"] == "TRANS_REFUND":
        return _process_huifu_refund_notification(
            fields=fields,
            payload_digest=payload_digest,
        )
    event_key = sha256(
        ":".join(
            (
                "payment",
                fields["huifu_id"],
                fields["req_date"],
                fields["req_seq_id"],
                fields["hf_seq_id"],
                fields["trans_type"],
                fields["notify_type"],
                fields["trans_stat"],
            )
        ).encode("utf-8")
    ).hexdigest()

    if not ProviderOrderPaymentOrder.objects.filter(
        req_date=fields["req_date"], req_seq_id=fields["req_seq_id"]
    ).exists():
        from activities.huifu import process_activity_huifu_payment_notification

        return process_activity_huifu_payment_notification(
            fields=fields,
            payload_digest=payload_digest,
        )

    with transaction.atomic():
        payment = (
            ProviderOrderPaymentOrder.objects.select_for_update()
            .select_related("order")
            .filter(req_date=fields["req_date"], req_seq_id=fields["req_seq_id"])
            .first()
        )
        if payment is None:
            raise ValidationError({"notification": "支付通知未匹配到本地支付单。"})
        if payment.gateway_merchant_id != fields["huifu_id"]:
            raise ValidationError({"notification": "支付通知商户号与本地支付单不一致。"})
        if fields["trans_amt"] and _huifu_amount_to_cents(fields["trans_amt"]) != payment.payable_amount:
            raise ValidationError({"notification": "支付通知金额与本地支付单不一致。"})
        event, _ = HuifuPaymentNotification.objects.get_or_create(
            event_key=event_key,
            defaults={
                "payment_order": payment,
                "huifu_id": fields["huifu_id"],
                "req_date": fields["req_date"],
                "req_seq_id": fields["req_seq_id"],
                "hf_seq_id": fields["hf_seq_id"],
                "trans_stat": fields["trans_stat"],
                "trans_amt": fields["trans_amt"],
                "trans_type": fields["trans_type"],
                "notify_type": fields["notify_type"],
                "payload_digest": payload_digest,
                "signature_verified": True,
            },
        )
        event = HuifuPaymentNotification.objects.select_for_update().get(pk=event.pk)
        if event.payload_digest != payload_digest or event.payment_order_id != payment.pk:
            raise ValidationError({"notification": "支付通知幂等键冲突。"})
        if event.status == HuifuPaymentNotification.Status.PROCESSED:
            if (
                event.trans_stat != "S"
                or payment.status
                in (
                    ProviderOrderPaymentOrder.Status.PAID,
                    ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
                    ProviderOrderPaymentOrder.Status.REFUNDED,
                )
            ):
                return f"RECV_ORD_ID_{fields['req_seq_id']}"
            raise HuifuGatewayError("支付通知已处理，但本地支付状态不一致。")
        if not event.query_req_seq_id:
            event.query_req_date = payment.req_date
            event.query_req_seq_id = payment.req_seq_id
            event.save(update_fields=("query_req_date", "query_req_seq_id"))
        gateway_trade_no = payment.gateway_trade_no or fields["hf_seq_id"]
        payment_id = payment.pk

    payment = ProviderOrderPaymentOrder.objects.select_related("order").get(pk=payment_id)
    query = _query_provider_order_huifu_payment(
        payment,
        fallback_hf_seq_id=gateway_trade_no,
    )
    _apply_huifu_payment_query_success(
        payment,
        query,
        fallback_hf_seq_id=fields["hf_seq_id"],
        fallback_end_time=fields["end_time"],
    )

    with transaction.atomic():
        event = HuifuPaymentNotification.objects.select_for_update().get(event_key=event_key)
        event.query_response_digest = query.response_digest
        event.status = HuifuPaymentNotification.Status.PROCESSED
        event.processed_at = timezone.now()
        event.save(
            update_fields=(
                "query_response_digest",
                "status",
                "processed_at",
            )
        )
    return f"RECV_ORD_ID_{fields['req_seq_id']}"


def _process_huifu_refund_notification(*, fields: dict, payload_digest: str) -> str:
    if not ProviderOrderRefundOrder.objects.filter(
        req_date=fields["req_date"], req_seq_id=fields["req_seq_id"]
    ).exists():
        from activities.huifu import process_activity_huifu_refund_notification

        return process_activity_huifu_refund_notification(
            fields=fields,
            payload_digest=payload_digest,
        )
    event_key = sha256(
        ":".join(
            (
                "refund",
                fields["huifu_id"],
                fields["req_date"],
                fields["req_seq_id"],
                fields["hf_seq_id"],
                fields["trans_stat"],
            )
        ).encode("utf-8")
    ).hexdigest()
    with transaction.atomic():
        refund = (
            ProviderOrderRefundOrder.objects.select_for_update()
            .filter(req_date=fields["req_date"], req_seq_id=fields["req_seq_id"])
            .first()
        )
        if refund is None:
            raise ValidationError({"notification": "退款通知未匹配到本地退款单。"})
        if refund.gateway_merchant_id != fields["huifu_id"]:
            raise ValidationError({"notification": "退款通知商户号与本地退款单不一致。"})
        if fields["ord_amt"] and (
            _huifu_amount_to_cents(fields["ord_amt"]) != refund.refund_amount
        ):
            raise ValidationError({"notification": "退款通知金额与本地退款单不一致。"})
        event, _ = HuifuRefundNotification.objects.get_or_create(
            event_key=event_key,
            defaults={
                "refund_order": refund,
                "huifu_id": fields["huifu_id"],
                "req_date": fields["req_date"],
                "req_seq_id": fields["req_seq_id"],
                "hf_seq_id": fields["hf_seq_id"],
                "trans_type": fields["trans_type"],
                "trans_stat": fields["trans_stat"],
                "ord_amt": fields["ord_amt"],
                "payload_digest": payload_digest,
                "signature_verified": True,
            },
        )
        event = HuifuRefundNotification.objects.select_for_update().get(pk=event.pk)
        if event.payload_digest != payload_digest or event.refund_order_id != refund.pk:
            raise ValidationError({"notification": "退款通知幂等键冲突。"})
        if event.status == HuifuRefundNotification.Status.PROCESSED:
            return f"RECV_ORD_ID_{fields['req_seq_id']}"
        refund_id = refund.pk
        fallback_hf_seq_id = refund.gateway_refund_no or fields["hf_seq_id"]

    refund = ProviderOrderRefundOrder.objects.get(pk=refund_id)
    query = _query_provider_order_huifu_refund(
        refund,
        fallback_hf_seq_id=fallback_hf_seq_id,
    )
    _apply_huifu_refund_query_result(
        refund,
        query,
        fallback_hf_seq_id=fields["hf_seq_id"],
    )
    with transaction.atomic():
        event = HuifuRefundNotification.objects.select_for_update().get(
            event_key=event_key
        )
        event.query_response_digest = query.response_digest
        event.status = HuifuRefundNotification.Status.PROCESSED
        event.processed_at = timezone.now()
        event.save(
            update_fields=(
                "query_response_digest",
                "status",
                "processed_at",
            )
        )
    return f"RECV_ORD_ID_{fields['req_seq_id']}"


@transaction.atomic
def apply_provider_order_payment_success(
    *,
    order_no: str,
    customer_id: int,
    channel: str,
    gateway_trade_no: str,
    paid_amount: int,
    signature_verified: bool,
    now=None,
):
    from providers.presence import operation_rules
    from taskcenter.services import (
        cancel_provider_order_payment_expiry,
        mark_provider_order_payment_expired,
        register_provider_acceptance_timeout,
    )

    now = now or timezone.now()
    order = ProviderOrder.objects.select_for_update().select_related("customer").get(
        order_no=order_no,
        customer_id=customer_id,
    )
    payment, _ = create_provider_order_payment_order(order)
    payment = ProviderOrderPaymentOrder.objects.select_for_update().get(pk=payment.pk)
    if not signature_verified:
        raise ValidationError({"signature": "支付结果签名校验未通过，已拒绝入账。"})
    if paid_amount != payment.payable_amount:
        raise ValidationError(
            {"paid_amount": "支付回调金额与订单应付金额不一致，已拒绝入账。"}
        )
    if (
        payment.status
        in (
            ProviderOrderPaymentOrder.Status.PAID,
            ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
            ProviderOrderPaymentOrder.Status.REFUNDED,
        )
        and order.paid_at
    ):
        return order, payment, False
    if order.status != ProviderOrder.Status.PENDING_PAYMENT:
        raise ValidationError({"status": "订单不在待支付状态。"})
    if order.payment_expires_at <= now:
        order.status = ProviderOrder.Status.CANCELLED
        order.cancelled_at = now
        order.save(update_fields=("status", "cancelled_at", "updated_at"))
        payment.status = ProviderOrderPaymentOrder.Status.CLOSED
        payment.closed_at = now
        payment.save(update_fields=("status", "closed_at", "updated_at"))
        mark_provider_order_payment_expired(order_no, source="payment_guard")
        return order, payment, False

    acceptance_timeout = operation_rules()["acceptance_timeout_minutes"]
    order.status = ProviderOrder.Status.PENDING_ACCEPTANCE
    order.paid_at = now
    order.acceptance_expires_at = now + timedelta(minutes=acceptance_timeout)
    order.save(update_fields=("status", "paid_at", "acceptance_expires_at", "updated_at"))
    payment.channel = channel
    payment.status = ProviderOrderPaymentOrder.Status.PAID
    payment.gateway_trade_no = gateway_trade_no
    payment.paid_at = now
    payment.save(update_fields=(
        "channel", "status", "gateway_trade_no", "paid_at", "updated_at",
    ))
    cancel_provider_order_payment_expiry(order_no, "payment_succeeded")
    register_provider_acceptance_timeout(order)
    create_order_notification(
        order=order,
        event_type=UserNotification.EventType.ORDER_PAYMENT_SUCCESS,
        title="订单支付成功",
        content="订单已进入待接单，达人会在接单时限内处理。",
    )
    create_provider_new_order_notification(order=order)
    return order, payment, True


def _discounted_order_components(order: ProviderOrder) -> dict[str, int]:
    remaining_discount = order.discount_amount
    service_amount = max(order.service_fee_amount - remaining_discount, 0)
    remaining_discount = max(remaining_discount - order.service_fee_amount, 0)
    other_amount = max(order.other_fee_amount - remaining_discount, 0)
    remaining_discount = max(remaining_discount - order.other_fee_amount, 0)
    transport_amount = max(order.transport_fee_amount - remaining_discount, 0)
    return {
        "service": service_amount,
        "transport": transport_amount,
        "other": other_amount,
    }
def _refund_allocation(order: ProviderOrder, amount: int) -> dict[str, int]:
    # Failed refunds remain retryable, so their amount must stay reserved to avoid
    # issuing another refund against the same paid balance.
    reserved = order.refund_orders.aggregate(
        service=Sum("service_fee_refund_amount"),
        transport=Sum("transport_fee_refund_amount"),
        other=Sum("other_fee_refund_amount"),
        total=Sum("refund_amount"),
    )
    components = _discounted_order_components(order)
    if amount <= 0:
        raise ValidationError({"approved_amount": "核准退款金额必须大于 0。"})
    if amount > order.payable_amount - (reserved["total"] or 0):
        raise ValidationError({"approved_amount": "核准退款金额超过当前可退金额。"})

    remaining = amount
    allocation = {"service": 0, "transport": 0, "other": 0}
    # Partial refunds preserve the provider's incurred transport fee until the end.
    for key in ("service", "other", "transport"):
        available = max(components[key] - (reserved[key] or 0), 0)
        allocated = min(remaining, available)
        allocation[key] = allocated
        remaining -= allocated
    if remaining:
        raise ValidationError({"approved_amount": "退款金额无法按订单费用构成分配。"})
    return allocation


@transaction.atomic
def create_customer_provider_order_after_sales_case(
    *, order_no: str, customer, case_type: str, requested_amount: int,
    reason: str, evidence_object_keys=None,
):
    from backoffice.models import ProviderOrderAfterSalesCase
    from taskcenter.services import (
        cancel_provider_order_confirmation_timeout,
        cancel_provider_order_settlement,
    )

    order = ProviderOrder.objects.select_for_update().filter(
        order_no=order_no, customer=customer
    ).first()
    if not order:
        raise ValidationError("订单不存在或无权操作。")
    if not order.paid_at:
        raise ValidationError("未支付订单不能申请退款或售后。")
    if order.status == ProviderOrder.Status.REFUNDED:
        raise ValidationError("该订单已经全额退款。")

    open_statuses = (
        ProviderOrderAfterSalesCase.Status.PENDING,
        ProviderOrderAfterSalesCase.Status.PROCESSING,
        ProviderOrderAfterSalesCase.Status.APPROVED,
    )
    existing = order.after_sales_cases.filter(status__in=open_statuses).first()
    if existing:
        return existing, False

    settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
    if settlement and settlement.status == ProviderOrderSettlement.Status.SETTLED:
        raise ValidationError("该订单资金已经结算，请联系平台客服处理。")

    reserved_amount = order.refund_orders.aggregate(total=Sum("refund_amount"))["total"] or 0
    refundable_amount = max(order.payable_amount - reserved_amount, 0)
    if requested_amount <= 0:
        raise ValidationError({"requested_amount": "退款申请金额必须大于 0。"})
    if requested_amount > refundable_amount:
        raise ValidationError(
            {"requested_amount": f"申请金额不能超过当前可退金额 {refundable_amount} 分。"}
        )

    original_status = order.status
    try:
        with transaction.atomic():
            case = ProviderOrderAfterSalesCase.objects.create(
                order=order,
                creator=customer,
                case_type=case_type,
                original_order_status=original_status,
                requested_amount=requested_amount,
                reason=reason.strip(),
                evidence_object_keys=evidence_object_keys or [],
            )
    except IntegrityError as error:
        existing = order.after_sales_cases.filter(status__in=open_statuses).first()
        if existing:
            return existing, False
        raise ValidationError("该订单已有未结束的退款或售后单。") from error

    order.status = ProviderOrder.Status.AFTER_SALES
    order.save(update_fields=("status", "updated_at"))
    if settlement and settlement.status == ProviderOrderSettlement.Status.RISK_FROZEN:
        settlement.status = ProviderOrderSettlement.Status.DISPUTE_FROZEN
        settlement.dispute_reason = f"存在待处理退款售后：{case.case_no}"
        settlement.save(update_fields=("status", "dispute_reason", "updated_at"))
        cancel_provider_order_settlement(order.order_no, "after_sales_processing")
    if original_status == ProviderOrder.Status.PENDING_CONFIRMATION:
        cancel_provider_order_confirmation_timeout(
            order.order_no, "after_sales_processing"
        )
    create_order_notification(
        order=order,
        event_type=UserNotification.EventType.ORDER_AFTER_SALES_STARTED,
        title="售后申请已提交",
        content="平台客服会尽快处理，结果将通过通知中心告知你。",
        dedupe_suffix=case.case_no,
    )
    return case, True


@transaction.atomic
def create_provider_order_refund(
    *,
    order_no: str,
    amount: int,
    source_type: str,
    source_reference: str,
    idempotency_key: str,
    reason: str,
    operator=None,
):
    from taskcenter.services import cancel_provider_order_settlement

    order = ProviderOrder.objects.select_for_update().select_related(
        "customer", "provider", "service__category"
    ).get(order_no=order_no)
    existing = ProviderOrderRefundOrder.objects.filter(
        idempotency_key=idempotency_key
    ).first()
    if existing:
        if existing.order_id != order.id or existing.refund_amount != amount:
            raise ValidationError("退款幂等键对应的业务参数不一致。")
        return existing, False
    payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
    if not payment or payment.status not in (
        ProviderOrderPaymentOrder.Status.PAID,
        ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
    ):
        raise ValidationError("订单缺少可退款支付单。")
    settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
    if settlement and settlement.status == ProviderOrderSettlement.Status.SETTLED:
        raise ValidationError("订单资金已经结算，不能直接退款，请转异常交易处理。")
    allocation = _refund_allocation(order, amount)
    try:
        with transaction.atomic():
            refund = ProviderOrderRefundOrder.objects.create(
                idempotency_key=idempotency_key,
                order=order,
                payment_order=payment,
                beneficiary=order.customer,
                source_type=source_type,
                source_reference=source_reference,
                service_fee_refund_amount=allocation["service"],
                transport_fee_refund_amount=allocation["transport"],
                other_fee_refund_amount=allocation["other"],
                refund_amount=amount,
                allocation_snapshot={
                    "version": "provider-refund-allocation-v1",
                    "priority": ["service", "other", "transport"],
                    "service_fee_refund_amount": allocation["service"],
                    "transport_fee_refund_amount": allocation["transport"],
                    "other_fee_refund_amount": allocation["other"],
                },
                reason=reason,
                operator=operator,
            )
    except IntegrityError as exc:
        existing = ProviderOrderRefundOrder.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing and existing.order_id == order.id and existing.refund_amount == amount:
            return existing, False
        raise ValidationError("退款请求发生并发冲突，请稍后重试。") from exc
    if settlement and settlement.status == ProviderOrderSettlement.Status.RISK_FROZEN:
        settlement.status = ProviderOrderSettlement.Status.DISPUTE_FROZEN
        settlement.dispute_reason = f"退款处理中：{source_reference}"
        settlement.save(update_fields=("status", "dispute_reason", "updated_at"))
        cancel_provider_order_settlement(order.order_no, "refund_processing")
    from taskcenter.services import register_provider_order_refund

    register_provider_order_refund(refund)
    return refund, True


def _settlement_amounts(order: ProviderOrder, commission_rate: Decimal) -> dict:
    components = _discounted_order_components(order)
    refunded = order.refund_orders.filter(
        status=ProviderOrderRefundOrder.Status.SUCCEEDED
    ).aggregate(
        service=Sum("service_fee_refund_amount"),
        transport=Sum("transport_fee_refund_amount"),
        other=Sum("other_fee_refund_amount"),
        total=Sum("refund_amount"),
    )
    net_service = max(components["service"] - (refunded["service"] or 0), 0)
    net_transport = max(components["transport"] - (refunded["transport"] or 0), 0)
    net_other = max(components["other"] - (refunded["other"] or 0), 0)
    platform_amount = int(
        (Decimal(net_service) * commission_rate / Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    provider_service_amount = net_service - platform_amount
    provider_amount = provider_service_amount + net_transport + net_other
    refunded_amount = refunded["total"] or 0
    return {
        "paid_amount": order.payable_amount,
        "refunded_amount": refunded_amount,
        "net_service_fee_amount": net_service,
        "net_transport_fee_amount": net_transport,
        "net_other_fee_amount": net_other,
        "platform_commission_rate": commission_rate,
        "platform_commission_amount": platform_amount,
        "provider_service_income_amount": provider_service_amount,
        "provider_settlement_amount": provider_amount,
        "calculation_snapshot": {
            "version": "provider-order-settlement-v1",
            "commission_basis": "net_service_fee",
            "transport_destination": "provider",
            "paid_amount": order.payable_amount,
            "refunded_amount": refunded_amount,
            "commission_rate": str(commission_rate),
        },
    }


def _apply_settlement_amounts(settlement, amounts):
    for field, value in amounts.items():
        setattr(settlement, field, value)


def _settlement_commission_rate(order: ProviderOrder) -> Decimal:
    value = order.pricing_snapshot.get("platform_commission_rate")
    try:
        rate = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        rate = order.service.category.platform_commission_rate
    if rate < 0 or rate > 100:
        return order.service.category.platform_commission_rate
    return rate


@transaction.atomic
def ensure_provider_order_settlement(*, order_no: str, now=None):
    from backoffice.operation_settings import platform_operation_rules
    from taskcenter.services import register_provider_order_settlement

    now = now or timezone.now()
    order = ProviderOrder.objects.select_for_update().select_related(
        "provider__user", "service__category"
    ).get(order_no=order_no)
    confirmed_at = order.customer_confirmed_at or order.auto_confirmed_at
    if not confirmed_at:
        raise ValidationError("订单尚未确认完成，不能进入结算。")
    payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
    if not payment or payment.status not in (
        ProviderOrderPaymentOrder.Status.PAID,
        ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
        ProviderOrderPaymentOrder.Status.REFUNDED,
    ):
        raise ValidationError("订单缺少已支付的支付单。")
    settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
    if settlement:
        return settlement, False
    rate = _settlement_commission_rate(order)
    amounts = _settlement_amounts(order, rate)
    freeze_until = confirmed_at + timedelta(
        days=platform_operation_rules()["provider_order_settlement_freeze_days"]
    )
    settlement = ProviderOrderSettlement.objects.create(
        order=order,
        provider=order.provider,
        frozen_at=confirmed_at,
        freeze_until=freeze_until,
        **amounts,
    )
    if settlement.refunded_amount >= settlement.paid_amount:
        settlement.status = ProviderOrderSettlement.Status.CANCELLED
        settlement.cancelled_at = now
        settlement.save(update_fields=("status", "cancelled_at", "updated_at"))
    else:
        register_provider_order_settlement(settlement)
    return settlement, True


@transaction.atomic
def advance_provider_order_settlement(*, order_no: str, now=None) -> dict:
    now = now or timezone.now()
    order = ProviderOrder.objects.select_for_update().select_related(
        "provider__user", "service__category"
    ).filter(order_no=order_no).first()
    if not order:
        return {"state": "missing", "order_no": order_no}
    settlement = ProviderOrderSettlement.objects.select_for_update().filter(
        order=order
    ).first()
    if not settlement:
        return {"state": "missing", "order_no": order_no}
    if settlement.status in (
        ProviderOrderSettlement.Status.SETTLED,
        ProviderOrderSettlement.Status.CANCELLED,
    ):
        return {"state": "not_applicable", "order_no": order_no, "status": settlement.status}
    from backoffice.models import ProviderOrderAfterSalesCase

    if ProviderOrderAfterSalesCase.objects.filter(
        order=order,
        status__in=(
            ProviderOrderAfterSalesCase.Status.PENDING,
            ProviderOrderAfterSalesCase.Status.PROCESSING,
            ProviderOrderAfterSalesCase.Status.APPROVED,
        ),
    ).exists():
        settlement.status = ProviderOrderSettlement.Status.DISPUTE_FROZEN
        settlement.dispute_reason = "存在待处理订单退款售后"
        settlement.save(update_fields=("status", "dispute_reason", "updated_at"))
        return {"state": "dispute_frozen", "order_no": order_no}
    amounts = _settlement_amounts(
        order, settlement.platform_commission_rate
    )
    _apply_settlement_amounts(settlement, amounts)
    if settlement.refunded_amount >= settlement.paid_amount:
        settlement.status = ProviderOrderSettlement.Status.CANCELLED
        settlement.cancelled_at = now
        settlement.dispute_reason = ""
        settlement.save()
        return {"state": "cancelled", "order_no": order_no}
    if now < settlement.freeze_until:
        settlement.status = ProviderOrderSettlement.Status.RISK_FROZEN
        settlement.dispute_reason = ""
        settlement.save()
        return {"state": "not_due", "order_no": order_no, "deadline": settlement.freeze_until}
    settlement.status = ProviderOrderSettlement.Status.SETTLED
    settlement.settled_at = now
    settlement.dispute_reason = ""
    settlement.save()
    create_notification(
        recipient=settlement.provider.user,
        category=UserNotification.Category.ORDER,
        event_type=UserNotification.EventType.PROVIDER_ORDER_SETTLED,
        title="订单收入已结算",
        content=(
            f"订单 {order_no} 收入 ¥{settlement.provider_settlement_amount // 100}."
            f"{settlement.provider_settlement_amount % 100:02d} 已完成平台账务结算，"
            "实际出款以资金账户记录为准。"
        ),
        target_type="provider_order_settlement",
        target_id=settlement.settlement_no,
        target_title=f"订单结算 {order_no}",
        action_text="查看收入",
        action_url="/pages/income/index",
        dedupe_key=f"provider-order-settlement:{settlement.settlement_no}:settled",
    )
    return {"state": "settled", "order_no": order_no}


def provider_order_refund_can_retry(refund, *, now=None) -> bool:
    now = now or timezone.now()
    if refund.status in (
        ProviderOrderRefundOrder.Status.PENDING,
        ProviderOrderRefundOrder.Status.FAILED,
    ):
        return True
    return (
        refund.status == ProviderOrderRefundOrder.Status.PROCESSING
        and refund.updated_at
        <= now - timedelta(seconds=settings.PAYMENT_REFUND_PROCESSING_TIMEOUT_SECONDS)
    )


def _complete_provider_order_refund(
    refund_no: str,
    *,
    gateway_refund_no: str,
    refunded_at,
):
    from taskcenter.services import (
        mark_provider_order_refund_succeeded,
        reopen_provider_order_settlement,
    )

    with transaction.atomic():
        refund_ref = ProviderOrderRefundOrder.objects.only("order_id").get(
            refund_no=refund_no
        )
        order = ProviderOrder.objects.select_for_update().select_related(
            "customer", "provider__user", "service__category"
        ).get(pk=refund_ref.order_id)
        payment = ProviderOrderPaymentOrder.objects.select_for_update().get(order=order)
        refund = ProviderOrderRefundOrder.objects.select_for_update().get(
            refund_no=refund_no
        )
        if refund.status == ProviderOrderRefundOrder.Status.SUCCEEDED:
            return refund, False
        refund.status = ProviderOrderRefundOrder.Status.SUCCEEDED
        refund.gateway_status = "S"
        refund.gateway_refund_no = gateway_refund_no or refund.gateway_refund_no
        refund.refunded_at = refunded_at
        refund.failure_reason = ""
        refund.save(
            update_fields=(
                "status",
                "gateway_status",
                "gateway_refund_no",
                "refunded_at",
                "failure_reason",
                "updated_at",
            )
        )
        refunded_total = order.refund_orders.filter(
            status=ProviderOrderRefundOrder.Status.SUCCEEDED
        ).aggregate(total=Sum("refund_amount"))["total"] or 0
        payment.status = (
            ProviderOrderPaymentOrder.Status.REFUNDED
            if refunded_total >= payment.payable_amount
            else ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED
        )
        payment.save(update_fields=("status", "updated_at"))

        from backoffice.models import ProviderOrderAfterSalesCase

        case = (
            ProviderOrderAfterSalesCase.objects.select_for_update()
            .filter(case_no=refund.source_reference, order=order)
            .first()
        )
        if case:
            case.status = ProviderOrderAfterSalesCase.Status.REFUNDED
            case.save(update_fields=("status", "updated_at"))
        if payment.status == ProviderOrderPaymentOrder.Status.REFUNDED:
            order.status = ProviderOrder.Status.REFUNDED
        elif case and order.status == ProviderOrder.Status.AFTER_SALES:
            order.status = case.original_order_status
        order.save(update_fields=("status", "updated_at"))

        settlement = ProviderOrderSettlement.objects.select_for_update().filter(
            order=order
        ).first()
        if settlement:
            amounts = _settlement_amounts(order, settlement.platform_commission_rate)
            _apply_settlement_amounts(settlement, amounts)
            if settlement.refunded_amount >= settlement.paid_amount:
                settlement.status = ProviderOrderSettlement.Status.CANCELLED
                settlement.cancelled_at = refunded_at
                settlement.dispute_reason = ""
                settlement.save()
            else:
                settlement.status = ProviderOrderSettlement.Status.RISK_FROZEN
                settlement.dispute_reason = ""
                settlement.save()
                reopen_provider_order_settlement(settlement)
        transaction.on_commit(
            lambda: create_order_notification(
                order=order,
                event_type=UserNotification.EventType.ORDER_REFUND_COMPLETED,
                title="订单退款成功",
                content=(
                    f"退款 ¥{refund.refund_amount // 100}."
                    f"{refund.refund_amount % 100:02d} 已按原支付路径退回。"
                ),
                dedupe_suffix=refund.refund_no,
            ),
            robust=True,
        )
        mark_provider_order_refund_succeeded(refund.refund_no)
        return refund, True


def _huifu_refunded_at(value: str, *, fallback):
    if not value:
        return fallback
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
    except (TypeError, ValueError) as exc:
        raise HuifuGatewayError("退款查询返回的完成时间无效。") from exc
    return timezone.make_aware(parsed, timezone.get_current_timezone())


def _query_provider_order_huifu_refund(
    refund: ProviderOrderRefundOrder,
    *,
    fallback_hf_seq_id: str = "",
) -> HuifuRefundQueryResult:
    gateway = get_huifu_payment_gateway()
    query = gateway.query_refund(
        req_date=refund.req_date,
        req_seq_id=refund.req_seq_id,
        refund_hf_seq_id=refund.gateway_refund_no or fallback_hf_seq_id,
    )
    if query.huifu_id != refund.gateway_merchant_id:
        raise HuifuGatewayError("退款查询返回的商户号与本地退款单不一致。")
    if query.ord_amt and _huifu_amount_to_cents(query.ord_amt) != refund.refund_amount:
        raise HuifuGatewayError("退款查询金额与本地退款单不一致。")
    if query.trans_stat == "S" and not query.ord_amt:
        raise HuifuGatewayError("退款成功查询未返回退款金额。")
    if (
        query.trans_stat == "S"
        and query.actual_ref_amt
        and _huifu_amount_to_cents(query.actual_ref_amt) != refund.refund_amount
    ):
        raise HuifuGatewayError("退款查询实际退款金额与本地退款单不一致。")
    queried_at = timezone.now()
    ProviderOrderRefundOrder.objects.filter(pk=refund.pk).update(
        gateway_refund_no=query.gateway_refund_no or refund.gateway_refund_no,
        gateway_status=query.trans_stat,
        gateway_last_query_status=query.trans_stat,
        gateway_last_query_digest=query.response_digest,
        gateway_last_queried_at=queried_at,
        updated_at=queried_at,
    )
    return query


def _apply_huifu_refund_query_result(
    refund: ProviderOrderRefundOrder,
    query: HuifuRefundQueryResult,
    *,
    fallback_hf_seq_id: str = "",
    now=None,
    raise_on_failure: bool = False,
):
    now = now or timezone.now()
    if query.trans_stat == "S":
        return _complete_provider_order_refund(
            refund.refund_no,
            gateway_refund_no=(
                query.gateway_refund_no or fallback_hf_seq_id or refund.gateway_refund_no
            ),
            refunded_at=_huifu_refunded_at(query.trans_finish_time, fallback=now),
        )
    if query.trans_stat == "F":
        ProviderOrderRefundOrder.objects.filter(pk=refund.pk).update(
            status=ProviderOrderRefundOrder.Status.FAILED,
            gateway_status="F",
            failure_reason="汇付退款终态失败，请人工核对后重试。",
            updated_at=now,
        )
        if raise_on_failure:
            raise HuifuRefundTerminalError()
    return ProviderOrderRefundOrder.objects.get(pk=refund.pk), False


def process_provider_order_refund(refund_no: str, *, now=None):
    from .payment_gateway import get_provider_order_payment_gateway

    now = now or timezone.now()
    with transaction.atomic():
        refund = (
            ProviderOrderRefundOrder.objects.select_for_update()
            .select_related("payment_order")
            .get(refund_no=refund_no)
        )
        if refund.status == ProviderOrderRefundOrder.Status.SUCCEEDED:
            return refund, False
        payment = refund.payment_order
        channel = payment.channel
        amount = refund.refund_amount
        if channel in (
            ProviderOrderPaymentOrder.Channel.MOCK_WECHAT,
            ProviderOrderPaymentOrder.Channel.MOCK_ALIPAY,
        ):
            if not provider_order_refund_can_retry(refund, now=now):
                raise ValidationError("退款正在处理中，请勿重复提交。")
            refund.status = ProviderOrderRefundOrder.Status.PROCESSING
            refund.failure_reason = ""
            refund.save(update_fields=("status", "failure_reason", "updated_at"))
            is_mock = True
            should_submit = True
        elif channel in (
            ProviderOrderPaymentOrder.Channel.WECHAT,
            ProviderOrderPaymentOrder.Channel.ALIPAY,
        ):
            if not payment.gateway_merchant_id or not payment.req_date or not payment.req_seq_id:
                raise ValidationError("原支付单缺少汇付交易定位信息，禁止自动退款。")
            refund.req_date = refund.req_date or timezone.localtime(now).strftime("%Y%m%d")
            refund.req_seq_id = refund.req_seq_id or refund.refund_no
            refund.gateway_merchant_id = payment.gateway_merchant_id
            refund.status = ProviderOrderRefundOrder.Status.PROCESSING
            refund.failure_reason = ""
            should_submit = not (
                refund.gateway_response_digest
                or refund.gateway_status
                or refund.gateway_refund_no
                or refund.gateway_last_query_digest
            )
            refund.save(
                update_fields=(
                    "req_date",
                    "req_seq_id",
                    "gateway_merchant_id",
                    "status",
                    "failure_reason",
                    "updated_at",
                )
            )
            is_mock = False
            request_values = {
                "req_date": refund.req_date,
                "req_seq_id": refund.req_seq_id,
                "org_req_date": payment.req_date,
                "org_req_seq_id": payment.req_seq_id,
                "org_hf_seq_id": payment.gateway_trade_no,
                "remark": refund.reason,
            }
        else:
            raise ValidationError("退款单支付渠道不支持自动退款。")

    if is_mock:
        try:
            result = get_provider_order_payment_gateway(channel).refund(
                refund_no=refund_no,
                amount=amount,
            )
        except Exception as exc:
            ProviderOrderRefundOrder.objects.filter(
                refund_no=refund_no,
                status=ProviderOrderRefundOrder.Status.PROCESSING,
            ).update(
                status=ProviderOrderRefundOrder.Status.FAILED,
                failure_reason=str(exc)[:1000],
                updated_at=timezone.now(),
            )
            raise
        return _complete_provider_order_refund(
            refund_no,
            gateway_refund_no=result.gateway_refund_no,
            refunded_at=now,
        )

    gateway = get_huifu_payment_gateway()
    if gateway.merchant_id != refund.gateway_merchant_id:
        raise HuifuGatewayError("退款商户配置与原支付单不一致。")
    fallback_hf_seq_id = refund.gateway_refund_no
    if should_submit:
        submitted = gateway.refund_payment(amount=amount, **request_values)
        if submitted.ord_amt and _huifu_amount_to_cents(submitted.ord_amt) != amount:
            raise HuifuGatewayError("退款受理金额与本地退款单不一致。")
        fallback_hf_seq_id = submitted.gateway_refund_no
        ProviderOrderRefundOrder.objects.filter(pk=refund.pk).update(
            gateway_refund_no=submitted.gateway_refund_no,
            gateway_status=submitted.trans_stat or "P",
            gateway_response_code=submitted.response_code,
            gateway_response_digest=submitted.response_digest,
            updated_at=timezone.now(),
        )

    refund = ProviderOrderRefundOrder.objects.get(pk=refund.pk)
    query = _query_provider_order_huifu_refund(
        refund,
        fallback_hf_seq_id=fallback_hf_seq_id,
    )
    return _apply_huifu_refund_query_result(
        refund,
        query,
        fallback_hf_seq_id=fallback_hf_seq_id,
        now=now,
        raise_on_failure=True,
    )


def refresh_provider_review_metrics(provider) -> None:
    aggregate = provider.order_reviews.filter(
        audit_status=ProviderOrderReview.AuditStatus.APPROVED,
        is_visible=True,
    ).aggregate(rating=Avg("rating"))
    provider.rating = aggregate["rating"] or Decimal("0.00")
    provider.service_count = ProviderOrder.objects.filter(
        provider=provider,
        status=ProviderOrder.Status.COMPLETED,
    ).count()
    provider.save(update_fields=("rating", "service_count", "updated_at"))


@dataclass(frozen=True)
class PriceQuote:
    service_fee_amount: int
    transport_fee_amount: int
    other_fee_amount: int
    discount_amount: int
    payable_amount: int
    snapshot: dict


def calculate_service_fee(service: ProviderService, duration_minutes: int) -> int:
    if service.billing_type == ProviderService.BillingType.PER_SESSION:
        return service.price_amount
    value = Decimal(service.price_amount) * Decimal(duration_minutes) / Decimal(60)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fallback_transport_fee(distance_km: Decimal | None) -> int:
    if distance_km is None or distance_km <= 0:
        return 0
    # PRD initial fallback: CNY 5 within 5 km, then CNY 5 for each additional 5 km.
    return max(1, math.ceil(float(distance_km) / 5)) * 500


def build_quote(service: ProviderService, duration_minutes: int, distance_km: Decimal | None) -> PriceQuote:
    service_fee = calculate_service_fee(service, duration_minutes)
    transport_fee = fallback_transport_fee(distance_km)
    total = service_fee + transport_fee
    return PriceQuote(
        service_fee_amount=service_fee,
        transport_fee_amount=transport_fee,
        other_fee_amount=0,
        discount_amount=0,
        payable_amount=total,
        snapshot={
            "version": "provider-order-pricing-v1",
            "currency": "CNY",
            "platform_commission_rate": str(service.category.platform_commission_rate),
            "time_grain_minutes": TIME_GRAIN_MINUTES,
            "minimum_hourly_minutes": MINIMUM_HOURLY_MINUTES,
            "transport_rule": "fallback_5_cny_per_5km" if distance_km is not None else "pending_map_route",
            "coupon": None,
        },
    )


def validate_booking(service: ProviderService, starts_at, duration_minutes: int):
    now = timezone.now()
    if starts_at < now + MINIMUM_ADVANCE:
        raise ValidationError({"starts_at": "至少提前1小时预约。"})
    if starts_at > now + MAXIMUM_ADVANCE:
        raise ValidationError({"starts_at": "最远仅可预约未来3天。"})
    if starts_at.minute % TIME_GRAIN_MINUTES or starts_at.second or starts_at.microsecond:
        raise ValidationError({"starts_at": "开始时间必须按30分钟粒度选择。"})
    if duration_minutes % TIME_GRAIN_MINUTES:
        raise ValidationError({"duration_minutes": "服务时长必须按30分钟递增。"})
    if service.billing_type == ProviderService.BillingType.HOURLY:
        if duration_minutes < MINIMUM_HOURLY_MINUTES:
            raise ValidationError({"duration_minutes": "按小时服务最低预约2小时。"})
    elif service.estimated_duration_minutes:
        duration_minutes = service.estimated_duration_minutes
    return duration_minutes, starts_at + timedelta(minutes=duration_minutes)


def ensure_slot_available(provider: ProviderProfile, starts_at, ends_at):
    now = timezone.now()
    blocking = Q(status=ProviderOrder.Status.PENDING_PAYMENT, payment_expires_at__gt=now) | Q(
        status__in=(
            ProviderOrder.Status.PENDING_ACCEPTANCE,
            ProviderOrder.Status.PENDING_SUPPORT,
            ProviderOrder.Status.PENDING_SERVICE,
            ProviderOrder.Status.DEPARTED,
            ProviderOrder.Status.IN_SERVICE,
            ProviderOrder.Status.PENDING_CONFIRMATION,
            ProviderOrder.Status.AFTER_SALES,
        )
    )
    if ProviderOrder.objects.filter(
        blocking, provider=provider, starts_at__lt=ends_at, ends_at__gt=starts_at
    ).exists():
        raise ValidationError({"starts_at": "该时间段刚刚被预约，请选择其他时间。"})


@transaction.atomic
def expire_provider_order_payment(order_no: str, *, now=None) -> dict:
    now = now or timezone.now()
    order = (
        ProviderOrder.objects.select_for_update(of=("self",))
        .filter(order_no=order_no)
        .only("order_no", "status", "payment_expires_at", "cancelled_at")
        .first()
    )
    if not order:
        return {"state": "missing", "order_no": order_no}
    if order.status != ProviderOrder.Status.PENDING_PAYMENT:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    if order.payment_expires_at > now:
        return {
            "state": "not_due",
            "order_no": order_no,
            "deadline": order.payment_expires_at,
        }
    order.status = ProviderOrder.Status.CANCELLED
    order.cancelled_at = now
    order.save(update_fields=("status", "cancelled_at", "updated_at"))
    payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
    if payment and payment.status == ProviderOrderPaymentOrder.Status.PENDING_PAYMENT:
        payment.status = ProviderOrderPaymentOrder.Status.CLOSED
        payment.closed_at = now
        payment.save(update_fields=("status", "closed_at", "updated_at"))
    return {"state": "expired", "order_no": order_no, "action": "cancelled"}


@transaction.atomic
def expire_provider_acceptance(order_no: str, *, now=None) -> dict:
    now = now or timezone.now()
    order = (
        ProviderOrder.objects.select_for_update()
        .filter(order_no=order_no)
        .select_related("customer")
        .first()
    )
    if not order:
        return {"state": "missing", "order_no": order_no}
    if order.status != ProviderOrder.Status.PENDING_ACCEPTANCE:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    if not order.acceptance_expires_at:
        return {"state": "invalid", "order_no": order_no, "reason": "missing_deadline"}
    if order.acceptance_expires_at > now:
        return {
            "state": "not_due",
            "order_no": order_no,
            "deadline": order.acceptance_expires_at,
        }
    order.status = ProviderOrder.Status.PENDING_SUPPORT
    order.save(update_fields=("status", "updated_at"))
    create_order_notification(
        order=order,
        event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
        title="订单已转客服处理",
        content="达人未在时限内接单，平台客服将继续协助处理。",
    )
    return {
        "state": "expired",
        "order_no": order_no,
        "action": "moved_to_support",
    }


@transaction.atomic
def expire_provider_rejection_support(order_no: str, *, now=None) -> dict:
    """Initiate the remaining full refund for an actively rejected order.

    Acceptance-timeout orders deliberately have no support deadline and never enter
    this flow. The refund itself stays on the shared asynchronous refund task path.
    """
    now = now or timezone.now()
    order = (
        ProviderOrder.objects.select_for_update(of=("self",))
        .filter(order_no=order_no)
        .select_related("customer", "payment_order")
        .first()
    )
    if not order:
        return {"state": "missing", "order_no": order_no}
    if not order.provider_rejected_at or not order.support_contact_deadline_at:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "reason": "not_provider_rejection_support",
        }
    if order.support_contacted_at:
        return {
            "state": "contacted",
            "order_no": order_no,
            "contacted_at": order.support_contacted_at.isoformat(),
        }
    if order.support_contact_deadline_at > now:
        return {
            "state": "not_due",
            "order_no": order_no,
            "deadline": order.support_contact_deadline_at,
        }

    payment = getattr(order, "payment_order", None)
    if order.status == ProviderOrder.Status.REFUNDED or (
        payment and payment.status == ProviderOrderPaymentOrder.Status.REFUNDED
    ):
        if order.status != ProviderOrder.Status.REFUNDED:
            order.status = ProviderOrder.Status.REFUNDED
            order.save(update_fields=("status", "updated_at"))
        return {"state": "already_refunded", "order_no": order_no, "action": "closed"}
    if order.status != ProviderOrder.Status.PENDING_SUPPORT:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    if not payment or payment.status not in (
        ProviderOrderPaymentOrder.Status.PAID,
        ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
    ):
        return {"state": "invalid", "order_no": order_no, "reason": "missing_payment"}

    reference = provider_rejection_refund_reference(order_no)
    existing = order.refund_orders.filter(idempotency_key=reference).first()
    if existing:
        return {
            "state": "refund_requested",
            "order_no": order_no,
            "refund_no": existing.refund_no,
            "refund_status": existing.status,
            "created": False,
        }

    reserved_amount = order.refund_orders.aggregate(total=Sum("refund_amount"))["total"] or 0
    remaining_amount = max(order.payable_amount - reserved_amount, 0)
    if remaining_amount == 0:
        if order.payable_amount == 0:
            payment.status = ProviderOrderPaymentOrder.Status.REFUNDED
            payment.save(update_fields=("status", "updated_at"))
            order.status = ProviderOrder.Status.REFUNDED
            order.save(update_fields=("status", "updated_at"))
            transaction.on_commit(
                lambda: create_order_notification(
                    order=order,
                    event_type=UserNotification.EventType.ORDER_REFUND_COMPLETED,
                    title="订单已关闭",
                    content="该订单无需退款，系统已关闭待客服处理流程。",
                    dedupe_suffix="provider-rejection-zero-amount",
                ),
                robust=True,
            )
            return {"state": "zero_amount_closed", "order_no": order_no, "action": "closed"}
        return {
            "state": "refund_reserved",
            "order_no": order_no,
            "reserved_amount": reserved_amount,
        }

    refund, created = create_provider_order_refund(
        order_no=order_no,
        amount=remaining_amount,
        source_type=ProviderOrderRefundOrder.SourceType.SYSTEM,
        source_reference=reference,
        idempotency_key=reference,
        reason="达人主动拒单且客服未在15分钟内完成有效联系，系统自动退款。",
    )
    return {
        "state": "refund_requested",
        "order_no": order_no,
        "refund_no": refund.refund_no,
        "refund_status": refund.status,
        "refund_amount": refund.refund_amount,
        "created": created,
    }


@transaction.atomic
def auto_confirm_provider_order(order_no: str, *, now=None) -> dict:
    now = now or timezone.now()
    order = (
        ProviderOrder.objects.select_for_update()
        .filter(order_no=order_no)
        .select_related("customer")
        .first()
    )
    if not order:
        return {"state": "missing", "order_no": order_no}
    if order.status != ProviderOrder.Status.PENDING_CONFIRMATION:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    if not order.confirmation_expires_at:
        return {"state": "invalid", "order_no": order_no, "reason": "missing_deadline"}
    if order.confirmation_expires_at > now:
        return {
            "state": "not_due",
            "order_no": order_no,
            "deadline": order.confirmation_expires_at,
        }
    order.status = ProviderOrder.Status.PENDING_REVIEW
    order.auto_confirmed_at = now
    order.save(update_fields=("status", "auto_confirmed_at", "updated_at"))
    ensure_provider_order_settlement(order_no=order.order_no, now=now)
    create_order_notification(
        order=order,
        event_type=UserNotification.EventType.ORDER_AUTO_CONFIRMED,
        title="订单已自动确认完成",
        content="订单已按规则自动确认完成，可以前往订单详情评价本次服务。",
    )
    return {
        "state": "expired",
        "order_no": order_no,
        "action": "auto_confirmed",
    }
