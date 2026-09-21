from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from notifications.models import UserNotification
from notifications.services import create_activity_notification
from orders.huifu import (
    HuifuGatewayError,
    HuifuPaymentQueryResult,
    HuifuPaymentSessionInProgress,
    HuifuPaymentSessionResult,
    HuifuRefundQueryResult,
    HuifuRefundTerminalError,
    get_huifu_payment_gateway,
)

from .models import (
    Activity,
    ActivityHuifuNotification,
    ActivityHuifuPaymentOrder,
    ActivityHuifuRefundOrder,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivityRefundRecord,
)


PAYMENT_SESSION_STALE_AFTER = timedelta(seconds=30)
HUIFU_CLOSE_MINIMUM_AGE = timedelta(minutes=1)
PAYMENT_SCENE_TRADE_TYPES = {"official_account": "T_JSAPI"}


def _amount_to_cents(value: str) -> int:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HuifuGatewayError("支付通道返回的金额格式无效。") from exc
    cents = amount * Decimal("100")
    if cents != cents.to_integral_value() or cents < 0:
        raise HuifuGatewayError("支付通道返回的金额格式无效。")
    return int(cents)


def _paid_at(value: str):
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
    except (TypeError, ValueError) as exc:
        raise HuifuGatewayError("支付通道未返回有效的交易完成时间。") from exc
    return timezone.make_aware(parsed, timezone.get_current_timezone())


def _refunded_at(value: str, *, fallback):
    if not value:
        return fallback
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
    except (TypeError, ValueError) as exc:
        raise HuifuGatewayError("退款查询返回的完成时间无效。") from exc
    return timezone.make_aware(parsed, timezone.get_current_timezone())


def _stored_session(payment: ActivityHuifuPaymentOrder) -> HuifuPaymentSessionResult:
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


def _resolve_business_order(*, payment_kind: str, activity_id: int, user_id: int, lock=False):
    if payment_kind == "activity_publish":
        queryset = ActivityPublishOrder.objects.select_related("activity")
        if lock:
            queryset = queryset.select_for_update()
        order = queryset.filter(
            activity_id=activity_id,
            payer_id=user_id,
        ).order_by("-created_at", "-id").first()
        if order is None:
            raise ValidationError({"order": "活动发布支付单不存在或无权操作。"})
        return order
    if payment_kind == "activity_participation":
        queryset = ActivityParticipationPaymentOrder.objects.select_related(
            "participation__activity", "payer"
        )
        if lock:
            queryset = queryset.select_for_update()
        order = queryset.filter(
            participation__activity_id=activity_id,
            payer_id=user_id,
        ).order_by("-created_at", "-id").first()
        if order is None:
            raise ValidationError({"order": "活动报名支付单不存在或无权操作。"})
        return order
    raise ValidationError({"payment_kind": "活动支付类型无效。"})


def _gateway_payment_for_order(order, *, payment_kind: str, lock=False):
    queryset = ActivityHuifuPaymentOrder.objects
    if lock:
        queryset = queryset.select_for_update()
    if payment_kind == "activity_publish":
        return queryset.filter(publish_order=order).first()
    return queryset.filter(participation_order=order).first()


def _create_gateway_payment(order, *, payment_kind: str):
    values = {"publish_order": order} if payment_kind == "activity_publish" else {
        "participation_order": order
    }
    return ActivityHuifuPaymentOrder.objects.create(**values)


def _business_values(order, *, payment_kind: str) -> dict:
    if payment_kind == "activity_publish":
        return {
            "amount": order.payable_amount,
            "goods_desc": f"活动发布-{order.activity.title}",
            "attach": f"activity-publish:{order.order_no}",
            "expires_at": order.expires_at,
        }
    return {
        "amount": order.payable_amount,
        "goods_desc": f"活动报名-{order.participation.activity.title}",
        "attach": f"activity-participation:{order.order_no}",
        "expires_at": order.expires_at,
    }


def create_activity_huifu_payment_session(
    *,
    payment_kind: str,
    activity_id: int,
    user_id: int,
    payment_scene: str,
    sub_openid: str,
):
    from config.payment_capabilities import ensure_activity_real_payment_available

    ensure_activity_real_payment_available(payment_kind)
    try:
        trade_type = PAYMENT_SCENE_TRADE_TYPES[payment_scene]
    except KeyError as exc:
        raise ValidationError({"payment_scene": "当前活动支付场景尚未开放。"}) from exc
    if not sub_openid:
        raise ValidationError({"authorization": "请先完成微信服务号网页授权。"})
    gateway = get_huifu_payment_gateway()
    gateway.validate_for_payment(trade_type=trade_type)
    now = timezone.now()

    with transaction.atomic():
        order = _resolve_business_order(
            payment_kind=payment_kind,
            activity_id=activity_id,
            user_id=user_id,
            lock=True,
        )
        if order.status != order.Status.PENDING_PAYMENT:
            raise ValidationError({"status": "活动支付单不在待支付状态。"})
        if order.expires_at <= now:
            raise ValidationError({"status": "活动支付已超时，请重新创建支付单。"})
        payment = _gateway_payment_for_order(
            order, payment_kind=payment_kind, lock=True
        )
        if payment is None:
            payment = _create_gateway_payment(order, payment_kind=payment_kind)
        if (
            payment.preorder_status == ActivityHuifuPaymentOrder.PreorderStatus.READY
            and payment.payment_scene == payment_scene
            and payment.trade_type == trade_type
            and payment.payment_invoke_payload
        ):
            return _stored_session(payment), False
        if (
            payment.preorder_status == ActivityHuifuPaymentOrder.PreorderStatus.SUBMITTING
            and payment.preorder_requested_at
            and payment.preorder_requested_at > now - PAYMENT_SESSION_STALE_AFTER
        ):
            raise HuifuPaymentSessionInProgress()
        if payment.payment_scene and payment.payment_scene != payment_scene:
            raise ValidationError({"payment_scene": "当前支付单已绑定其他支付场景。"})

        payment.req_date = payment.req_date or timezone.localtime(now).strftime("%Y%m%d")
        payment.req_seq_id = payment.req_seq_id or order.order_no
        payment.gateway_merchant_id = gateway.merchant_id
        payment.payment_scene = payment_scene
        payment.trade_type = trade_type
        payment.preorder_status = ActivityHuifuPaymentOrder.PreorderStatus.SUBMITTING
        payment.preorder_requested_at = now
        payment.preorder_attempts += 1
        payment.gateway_response_code = ""
        payment.payment_invoke_payload = {}
        payment.save()
        values = _business_values(order, payment_kind=payment_kind)
        request_values = {
            "req_date": payment.req_date,
            "req_seq_id": payment.req_seq_id,
            "amount": values["amount"],
            "goods_desc": values["goods_desc"],
            "trade_type": trade_type,
            "attach": values["attach"],
            "time_expire": timezone.localtime(values["expires_at"]).strftime(
                "%Y%m%d%H%M%S"
            ),
            "sub_openid": sub_openid,
        }
        payment_id = payment.pk

    try:
        result = gateway.create_payment(**request_values)
    except HuifuGatewayError as exc:
        ActivityHuifuPaymentOrder.objects.filter(pk=payment_id).update(
            preorder_status=ActivityHuifuPaymentOrder.PreorderStatus.FAILED,
            gateway_response_code=exc.response_code[:32],
            gateway_response_digest=exc.response_digest[:64],
            updated_at=timezone.now(),
        )
        raise

    with transaction.atomic():
        payment = ActivityHuifuPaymentOrder.objects.select_for_update().get(pk=payment_id)
        if (
            payment.req_date != result.req_date
            or payment.req_seq_id != result.req_seq_id
        ):
            raise HuifuGatewayError("支付请求流水已变更，请重新进入支付页。")
        payment.preorder_status = ActivityHuifuPaymentOrder.PreorderStatus.READY
        payment.gateway_merchant_id = result.huifu_id
        payment.gateway_trade_no = result.hf_seq_id
        payment.gateway_party_order_id = result.party_order_id
        payment.gateway_out_trans_id = result.out_trans_id
        payment.payment_invoke_payload = result.pay_info
        payment.gateway_response_code = result.response_code
        payment.gateway_response_digest = result.response_digest
        payment.preorder_ready_at = timezone.now()
        payment.save()
        if payment.participation_order_id:
            ActivityParticipationPaymentOrder.objects.filter(
                pk=payment.participation_order_id
            ).update(channel=ActivityParticipationPaymentOrder.Channel.WECHAT)
        return _stored_session(payment), True


def _payment_business_order(payment: ActivityHuifuPaymentOrder):
    if payment.publish_order_id:
        return "activity_publish", payment.publish_order
    return "activity_participation", payment.participation_order


def _query_payment(
    payment: ActivityHuifuPaymentOrder,
    *,
    fallback_hf_seq_id: str = "",
) -> HuifuPaymentQueryResult:
    kind, order = _payment_business_order(payment)
    query = get_huifu_payment_gateway().query_payment(
        req_date=payment.req_date,
        req_seq_id=payment.req_seq_id,
        hf_seq_id=payment.gateway_trade_no or fallback_hf_seq_id,
    )
    if query.huifu_id != payment.gateway_merchant_id:
        raise HuifuGatewayError("支付查询返回的商户号与本地支付单不一致。")
    if query.trans_amt:
        if _amount_to_cents(query.trans_amt) != order.payable_amount:
            raise HuifuGatewayError("支付查询金额与本地支付单不一致。")
    elif query.trans_stat == "S":
        raise HuifuGatewayError("支付成功查询未返回交易金额。")
    if query.trade_type and payment.trade_type and query.trade_type != payment.trade_type:
        raise HuifuGatewayError("支付查询返回的交易类型与本地支付单不一致。")
    ActivityHuifuPaymentOrder.objects.filter(pk=payment.pk).update(
        gateway_last_query_status=query.trans_stat,
        gateway_last_query_digest=query.response_digest,
        gateway_last_queried_at=timezone.now(),
        gateway_trade_no=query.gateway_trade_no or payment.gateway_trade_no,
        updated_at=timezone.now(),
    )
    return query


def _apply_publish_payment_success(order_id: int, *, gateway_trade_no: str, paid_at):
    from taskcenter.services import cancel_activity_publish_payment_expiry

    needs_refund = False
    with transaction.atomic():
        order = ActivityPublishOrder.objects.select_for_update().select_related(
            "activity", "payer"
        ).get(pk=order_id)
        activity = Activity.objects.select_for_update().get(pk=order.activity_id)
        if order.status in (
            ActivityPublishOrder.Status.PAID,
            ActivityPublishOrder.Status.PARTIALLY_REFUNDED,
            ActivityPublishOrder.Status.REFUNDED,
        ):
            return order, False, False
        late = (
            order.status == ActivityPublishOrder.Status.CANCELLED
            or order.expires_at <= paid_at
        )
        if order.status not in (
            ActivityPublishOrder.Status.PENDING_PAYMENT,
            ActivityPublishOrder.Status.CANCELLED,
        ):
            raise HuifuGatewayError("活动发布支付单状态异常，需要人工核对。")
        order.status = ActivityPublishOrder.Status.PAID
        order.paid_at = paid_at
        order.closed_at = None
        order.save(update_fields=("status", "paid_at", "closed_at", "updated_at"))
        cancel_activity_publish_payment_expiry(order.order_no, "发布支付成功")
        if late:
            needs_refund = True
        else:
            activity.status = Activity.Status.PENDING_REVIEW
            activity.save(update_fields=("status", "updated_at"))
            transaction.on_commit(
                lambda: create_activity_notification(
                    activity=activity,
                    recipient=order.payer,
                    event_type=UserNotification.EventType.ACTIVITY_PUBLISH_SUBMITTED,
                    title="活动已提交审核",
                    content="活动提交成功，平台正在审核中，可在“我的活动”查看进展。",
                    dedupe_suffix=order.order_no,
                    action_text="查看进展",
                    action_url="/pages/activities/mine?role=organized",
                ),
                robust=True,
            )
    if needs_refund:
        from .services import refund_publish_order

        refund_publish_order(
            activity=order.activity,
            publish_order=order,
            refund_type=ActivityRefundRecord.RefundType.ADMIN_CANCELLATION,
            reason="活动发布支付超时后到账，系统自动原路退款。",
            operator=None,
        )
    return order, True, needs_refund


def _apply_participation_payment_success(
    order_id: int, *, gateway_trade_no: str, paid_at
):
    from .services import (
        create_activity_participation_refund,
        sync_activity_formation_status,
    )
    from taskcenter.services import cancel_activity_participation_payment_expiry

    needs_refund = False
    with transaction.atomic():
        order = (
            ActivityParticipationPaymentOrder.objects.select_for_update()
            .select_related("participation__activity", "participation__user")
            .get(pk=order_id)
        )
        participation = ActivityParticipation.objects.select_for_update().get(
            pk=order.participation_id
        )
        activity = Activity.objects.select_for_update().get(pk=participation.activity_id)
        if order.status in (
            ActivityParticipationPaymentOrder.Status.PAID,
            ActivityParticipationPaymentOrder.Status.PARTIALLY_REFUNDED,
            ActivityParticipationPaymentOrder.Status.REFUNDED,
        ):
            return order, False, False
        late = (
            order.status == ActivityParticipationPaymentOrder.Status.CLOSED
            or order.expires_at <= paid_at
            or participation.status
            in (
                ActivityParticipation.Status.CANCELLED,
                ActivityParticipation.Status.EXPIRED,
            )
        )
        if order.status not in (
            ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT,
            ActivityParticipationPaymentOrder.Status.CLOSED,
        ):
            raise HuifuGatewayError("活动报名支付单状态异常，需要人工核对。")
        order.status = ActivityParticipationPaymentOrder.Status.PAID
        order.channel = ActivityParticipationPaymentOrder.Channel.WECHAT
        order.gateway_trade_no = gateway_trade_no
        order.paid_at = paid_at
        order.closed_at = None
        order.save(
            update_fields=(
                "status", "channel", "gateway_trade_no", "paid_at", "closed_at",
                "updated_at",
            )
        )
        cancel_activity_participation_payment_expiry(order.order_no, "报名支付成功")
        if late:
            needs_refund = True
        else:
            participation.status = ActivityParticipation.Status.ACTIVE
            participation.joined_at = paid_at
            participation.payment_expires_at = None
            participation.save(
                update_fields=(
                    "status", "joined_at", "payment_expires_at", "updated_at"
                )
            )
            participant_count = ActivityParticipation.objects.filter(
                activity=activity,
                status=ActivityParticipation.Status.ACTIVE,
            ).count()
            sync_activity_formation_status(activity, participant_count)
            transaction.on_commit(
                lambda: create_activity_notification(
                    activity=activity,
                    recipient=participation.user,
                    event_type=UserNotification.EventType.ACTIVITY_SIGNUP_SUCCESS,
                    title="活动报名成功",
                    content="报名支付成功，活动名额已经为你保留。",
                    dedupe_suffix=str(participation.pk),
                ),
                robust=True,
            )
            transaction.on_commit(
                lambda: create_activity_notification(
                    activity=activity,
                    recipient=activity.organizer,
                    event_type=UserNotification.EventType.ACTIVITY_SIGNUP_SUCCESS,
                    title="活动有新成员报名",
                    content=(
                        f"{participation.user.nickname or '一位用户'}已完成报名，"
                        f"当前共有 {participant_count} 位参与者。"
                    ),
                    dedupe_suffix=f"organizer-{participation.pk}",
                ),
                robust=True,
            )
    if needs_refund:
        create_activity_participation_refund(
            participation=order.participation,
            payment_order=order,
            refund_type=ActivityParticipationRefundOrder.RefundType.PAYMENT_TIMEOUT,
            idempotency_key=f"activity-payment-timeout:{order.order_no}",
            principal_refund_amount=order.aa_principal_amount,
            service_fee_refund_amount=order.platform_service_fee_amount,
            reason="活动报名支付超时或名额释放后到账，系统自动原路退款。",
        )
    return order, True, needs_refund


def _apply_payment_success(
    payment: ActivityHuifuPaymentOrder,
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
    paid_at = _paid_at(query.end_time or fallback_end_time)
    if payment.publish_order_id:
        return _apply_publish_payment_success(
            payment.publish_order_id,
            gateway_trade_no=gateway_trade_no,
            paid_at=paid_at,
        )
    return _apply_participation_payment_success(
        payment.participation_order_id,
        gateway_trade_no=gateway_trade_no,
        paid_at=paid_at,
    )


def confirm_activity_huifu_payment_status(
    *, payment_kind: str, activity_id: int, user_id: int
) -> dict:
    order = _resolve_business_order(
        payment_kind=payment_kind,
        activity_id=activity_id,
        user_id=user_id,
    )
    payment = _gateway_payment_for_order(order, payment_kind=payment_kind)
    if payment is None or not payment.req_date or not payment.req_seq_id:
        return {"state": "not_started", "order_no": order.order_no}
    if payment_kind == "activity_publish":
        refund = ActivityRefundRecord.objects.filter(publish_order=order).first()
        refund_succeeded_statuses = (
            ActivityRefundRecord.Status.SUCCEEDED,
            ActivityRefundRecord.Status.SIMULATED_REFUNDED,
        )
    else:
        refund = ActivityParticipationRefundOrder.objects.filter(
            payment_order=order,
            refund_type=ActivityParticipationRefundOrder.RefundType.PAYMENT_TIMEOUT,
        ).order_by("-created_at", "-id").first()
        refund_succeeded_statuses = (
            ActivityParticipationRefundOrder.Status.SUCCEEDED,
        )
    if refund and refund.status in (
        refund.Status.PENDING,
        refund.Status.PROCESSING,
    ):
        return {
            "state": "refund_pending",
            "order_no": order.order_no,
            "changed": False,
        }
    if refund and refund.status in refund_succeeded_statuses:
        return {
            "state": "refunded",
            "order_no": order.order_no,
            "changed": False,
        }
    if refund and refund.status == refund.Status.FAILED:
        return {
            "state": "refund_failed",
            "order_no": order.order_no,
            "changed": False,
        }
    if order.status in (
        order.Status.PAID,
        order.Status.PARTIALLY_REFUNDED,
    ):
        return {"state": "paid", "order_no": order.order_no, "changed": False}
    if order.status == order.Status.REFUNDED:
        return {"state": "refunded", "order_no": order.order_no, "changed": False}
    query = _query_payment(payment)
    applied = _apply_payment_success(payment, query)
    if applied is not None:
        return {
            "state": "refund_pending" if applied[2] else "paid",
            "order_no": order.order_no,
            "changed": applied[1],
            "trans_stat": query.trans_stat,
        }
    return {
        "state": "failed" if query.trans_stat == "F" else "processing",
        "order_no": order.order_no,
        "trans_stat": query.trans_stat,
    }


def _cancel_compensation_refund_state(*, payment_kind: str, order) -> dict | None:
    if payment_kind == "activity_publish":
        refund = ActivityRefundRecord.objects.filter(publish_order=order).first()
        succeeded_statuses = (
            ActivityRefundRecord.Status.SUCCEEDED,
            ActivityRefundRecord.Status.SIMULATED_REFUNDED,
        )
    else:
        refund = ActivityParticipationRefundOrder.objects.filter(
            payment_order=order,
            refund_type=ActivityParticipationRefundOrder.RefundType.PAYMENT_TIMEOUT,
        ).order_by("-created_at", "-id").first()
        succeeded_statuses = (ActivityParticipationRefundOrder.Status.SUCCEEDED,)
    if refund is None:
        return None
    if refund.status in succeeded_statuses:
        state = "refunded"
    elif refund.status == refund.Status.FAILED:
        state = "refund_failed"
    else:
        state = "refund_processing"
    return {
        "state": state,
        "order_no": order.order_no,
        "refund_no": refund.refund_no,
        "refund_status": refund.status,
    }


def _ensure_cancel_compensation_refund(*, payment_kind: str, order) -> dict:
    refund_state = _cancel_compensation_refund_state(
        payment_kind=payment_kind,
        order=order,
    )
    if refund_state is not None:
        return refund_state
    if payment_kind == "activity_publish":
        from .services import refund_publish_order

        refund_publish_order(
            activity=order.activity,
            publish_order=order,
            refund_type=ActivityRefundRecord.RefundType.ADMIN_CANCELLATION,
            reason="活动发布支付超时或取消后到账，系统自动原路退款。",
            operator=None,
        )
    else:
        from .services import create_activity_participation_refund

        create_activity_participation_refund(
            participation=order.participation,
            payment_order=order,
            refund_type=ActivityParticipationRefundOrder.RefundType.PAYMENT_TIMEOUT,
            idempotency_key=f"activity-payment-timeout:{order.order_no}",
            principal_refund_amount=order.aa_principal_amount,
            service_fee_refund_amount=order.platform_service_fee_amount,
            reason="活动报名支付超时或名额释放后到账，系统自动原路退款。",
        )
    return _cancel_compensation_refund_state(
        payment_kind=payment_kind,
        order=order,
    ) or {"state": "refund_processing", "order_no": order.order_no}


def _save_huifu_close_result(payment_id: int, result, *, queried: bool) -> None:
    values = {
        "gateway_close_status": result.trans_stat,
        "gateway_close_response_code": result.response_code,
        "gateway_close_response_digest": result.response_digest,
        "updated_at": timezone.now(),
    }
    if queried:
        values["gateway_close_queried_at"] = timezone.now()
    ActivityHuifuPaymentOrder.objects.filter(pk=payment_id).update(**values)


def process_activity_huifu_cancel_compensation(
    *, payment_kind: str, order_no: str, now=None
) -> dict:
    """Query before close; automatically refund a payment won by a cancel race."""

    now = now or timezone.now()
    if payment_kind == "activity_publish":
        order = ActivityPublishOrder.objects.select_related("activity").filter(
            order_no=order_no
        ).first()
        closed_status = ActivityPublishOrder.Status.CANCELLED
    elif payment_kind == "activity_participation":
        order = ActivityParticipationPaymentOrder.objects.select_related(
            "participation__activity"
        ).filter(order_no=order_no).first()
        closed_status = ActivityParticipationPaymentOrder.Status.CLOSED
    else:
        raise ValidationError({"payment_kind": "活动支付类型无效。"})
    if order is None:
        return {"state": "missing", "order_no": order_no}

    refund_state = _cancel_compensation_refund_state(
        payment_kind=payment_kind,
        order=order,
    )
    if refund_state is not None:
        return refund_state
    if order.status == order.Status.REFUNDED:
        return {"state": "refunded", "order_no": order_no}
    if order.status in (order.Status.PAID, order.Status.PARTIALLY_REFUNDED):
        is_late_payment = (
            order.activity.status == Activity.Status.DRAFT
            if payment_kind == "activity_publish"
            else order.participation.status
            in (
                ActivityParticipation.Status.CANCELLED,
                ActivityParticipation.Status.EXPIRED,
            )
        )
        if not is_late_payment:
            return {
                "state": "not_applicable",
                "order_no": order_no,
                "payment_status": order.status,
            }
        return _ensure_cancel_compensation_refund(
            payment_kind=payment_kind,
            order=order,
        )
    if order.status != closed_status:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "payment_status": order.status,
        }

    payment = _gateway_payment_for_order(order, payment_kind=payment_kind)
    if payment is None or not (
        payment.gateway_merchant_id and payment.req_date and payment.req_seq_id
    ):
        return {"state": "closed", "order_no": order_no, "source": "local"}

    query = _query_payment(payment)
    if query.trans_stat == "S":
        _apply_payment_success(payment, query)
        order.refresh_from_db()
        return _ensure_cancel_compensation_refund(
            payment_kind=payment_kind,
            order=order,
        )
    if query.trans_stat == "F":
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
        locked_payment = ActivityHuifuPaymentOrder.objects.select_for_update().get(
            pk=payment.pk
        )
        request_date = timezone.localtime(now).strftime("%Y%m%d")
        if not locked_payment.close_req_seq_id:
            locked_payment.close_req_date = request_date
            locked_payment.close_req_seq_id = f"{order.order_no}CLOSE"
        if not locked_payment.close_query_req_seq_id:
            locked_payment.close_query_req_date = request_date
            locked_payment.close_query_req_seq_id = f"{order.order_no}CLOSEQ"
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
    if close_result.huifu_id != payment.gateway_merchant_id:
        raise HuifuGatewayError("关单返回的商户号与本地支付单不一致。")
    _save_huifu_close_result(
        payment.pk,
        close_result,
        queried=query_existing_close,
    )
    if close_result.trans_stat == "S" or (
        close_result.trans_stat == "F" and close_result.org_trans_stat == "F"
    ):
        return {"state": "closed", "order_no": order_no, "source": "gateway_close"}
    return {
        "state": "processing",
        "order_no": order_no,
        "trans_stat": close_result.trans_stat,
        "org_trans_stat": close_result.org_trans_stat,
    }


def process_activity_huifu_payment_notification(
    *, fields: dict, payload_digest: str
) -> str:
    event_key = sha256(
        ":".join(
            (
                "activity-payment",
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
    with transaction.atomic():
        payment = (
            ActivityHuifuPaymentOrder.objects.select_for_update(of=("self",))
            .select_related("publish_order", "participation_order")
            .filter(req_date=fields["req_date"], req_seq_id=fields["req_seq_id"])
            .first()
        )
        if payment is None:
            raise ValidationError({"notification": "支付通知未匹配到本地活动支付单。"})
        if payment.gateway_merchant_id != fields["huifu_id"]:
            raise ValidationError({"notification": "支付通知商户号与本地支付单不一致。"})
        _, order = _payment_business_order(payment)
        if fields["trans_amt"] and _amount_to_cents(fields["trans_amt"]) != order.payable_amount:
            raise ValidationError({"notification": "支付通知金额与本地支付单不一致。"})
        event, _ = ActivityHuifuNotification.objects.get_or_create(
            event_key=event_key,
            defaults={
                "payment": payment,
                "huifu_id": fields["huifu_id"],
                "req_date": fields["req_date"],
                "req_seq_id": fields["req_seq_id"],
                "hf_seq_id": fields["hf_seq_id"],
                "trans_type": fields["trans_type"],
                "notify_type": fields["notify_type"],
                "trans_stat": fields["trans_stat"],
                "amount": fields["trans_amt"],
                "payload_digest": payload_digest,
                "signature_verified": True,
            },
        )
        event = ActivityHuifuNotification.objects.select_for_update().get(pk=event.pk)
        if event.payload_digest != payload_digest or event.payment_id != payment.pk:
            raise ValidationError({"notification": "支付通知幂等键冲突。"})
        if event.status == ActivityHuifuNotification.Status.PROCESSED:
            return f"RECV_ORD_ID_{fields['req_seq_id']}"
        payment_id = payment.pk
        fallback_hf_seq_id = payment.gateway_trade_no or fields["hf_seq_id"]

    payment = ActivityHuifuPaymentOrder.objects.select_related(
        "publish_order", "participation_order"
    ).get(pk=payment_id)
    query = _query_payment(payment, fallback_hf_seq_id=fallback_hf_seq_id)
    _apply_payment_success(
        payment,
        query,
        fallback_hf_seq_id=fields["hf_seq_id"],
        fallback_end_time=fields["end_time"],
    )
    ActivityHuifuNotification.objects.filter(event_key=event_key).update(
        query_response_digest=query.response_digest,
        status=ActivityHuifuNotification.Status.PROCESSED,
        processed_at=timezone.now(),
    )
    return f"RECV_ORD_ID_{fields['req_seq_id']}"


def _refund_business(refund_request: ActivityHuifuRefundOrder):
    if refund_request.publish_refund_id:
        refund = refund_request.publish_refund
        payment = refund.publish_order.huifu_payment
        return "activity_publish", refund, payment
    refund = refund_request.participation_refund
    payment = refund.payment_order.huifu_payment
    return "activity_participation", refund, payment


def _lock_or_create_refund_request(*, payment_kind: str, refund):
    queryset = ActivityHuifuRefundOrder.objects.select_for_update()
    if payment_kind == "activity_publish":
        existing = queryset.filter(publish_refund=refund).first()
        return existing or ActivityHuifuRefundOrder.objects.create(
            publish_refund=refund
        )
    existing = queryset.filter(participation_refund=refund).first()
    return existing or ActivityHuifuRefundOrder.objects.create(
        participation_refund=refund
    )


def _query_refund(
    refund_request: ActivityHuifuRefundOrder,
    *,
    fallback_hf_seq_id: str = "",
) -> HuifuRefundQueryResult:
    _kind, refund, _payment = _refund_business(refund_request)
    query = get_huifu_payment_gateway().query_refund(
        req_date=refund_request.req_date,
        req_seq_id=refund_request.req_seq_id,
        refund_hf_seq_id=refund_request.gateway_refund_no or fallback_hf_seq_id,
    )
    if query.huifu_id != refund_request.gateway_merchant_id:
        raise HuifuGatewayError("退款查询返回的商户号与本地退款单不一致。")
    if query.ord_amt and _amount_to_cents(query.ord_amt) != refund.refund_amount:
        raise HuifuGatewayError("退款查询金额与本地退款单不一致。")
    if query.trans_stat == "S" and not query.ord_amt:
        raise HuifuGatewayError("退款成功查询未返回退款金额。")
    if (
        query.trans_stat == "S"
        and query.actual_ref_amt
        and _amount_to_cents(query.actual_ref_amt) != refund.refund_amount
    ):
        raise HuifuGatewayError("退款查询实际退款金额与本地退款单不一致。")
    queried_at = timezone.now()
    ActivityHuifuRefundOrder.objects.filter(pk=refund_request.pk).update(
        gateway_refund_no=query.gateway_refund_no or refund_request.gateway_refund_no,
        gateway_status=query.trans_stat,
        gateway_last_query_status=query.trans_stat,
        gateway_last_query_digest=query.response_digest,
        gateway_last_queried_at=queried_at,
        updated_at=queried_at,
    )
    return query


def _apply_refund_query_result(
    refund_request: ActivityHuifuRefundOrder,
    query: HuifuRefundQueryResult,
    *,
    fallback_hf_seq_id: str = "",
    now=None,
    raise_on_failure=False,
):
    from .services import (
        complete_activity_participation_refund,
        complete_activity_publish_refund,
    )

    now = now or timezone.now()
    payment_kind, refund, _payment = _refund_business(refund_request)
    if query.trans_stat == "S":
        gateway_refund_no = (
            query.gateway_refund_no
            or fallback_hf_seq_id
            or refund_request.gateway_refund_no
        )
        completed_at = _refunded_at(query.trans_finish_time, fallback=now)
        if payment_kind == "activity_publish":
            return complete_activity_publish_refund(
                refund.refund_no,
                gateway_refund_no=gateway_refund_no,
                refunded_at=completed_at,
            )
        return complete_activity_participation_refund(
            refund.refund_no,
            gateway_refund_no=gateway_refund_no,
            refunded_at=completed_at,
        )
    if query.trans_stat == "F":
        if payment_kind == "activity_publish":
            ActivityRefundRecord.objects.filter(pk=refund.pk).update(
                status=ActivityRefundRecord.Status.FAILED,
                failure_reason="汇付退款终态失败，请人工核对后重试。",
                updated_at=now,
            )
        else:
            ActivityParticipationRefundOrder.objects.filter(pk=refund.pk).update(
                status=ActivityParticipationRefundOrder.Status.FAILED,
                failure_reason="汇付退款终态失败，请人工核对后重试。",
                updated_at=now,
            )
        if raise_on_failure:
            raise HuifuRefundTerminalError()
    if payment_kind == "activity_publish":
        return ActivityRefundRecord.objects.get(pk=refund.pk), False
    return ActivityParticipationRefundOrder.objects.get(pk=refund.pk), False


def _process_refund(*, payment_kind: str, refund_no: str, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        if payment_kind == "activity_publish":
            refund = (
                ActivityRefundRecord.objects.select_for_update()
                .select_related("publish_order")
                .get(refund_no=refund_no)
            )
            if refund.status in (
                ActivityRefundRecord.Status.SUCCEEDED,
                ActivityRefundRecord.Status.SIMULATED_REFUNDED,
            ):
                return refund, False
            try:
                payment = refund.publish_order.huifu_payment
            except ActivityHuifuPaymentOrder.DoesNotExist as exc:
                raise ValidationError("原支付单缺少汇付交易定位信息，禁止自动退款。") from exc
            refund.status = ActivityRefundRecord.Status.PROCESSING
            refund.failure_reason = ""
            refund.save(update_fields=("status", "failure_reason", "updated_at"))
        else:
            refund = (
                ActivityParticipationRefundOrder.objects.select_for_update()
                .select_related("payment_order")
                .get(refund_no=refund_no)
            )
            if refund.status == ActivityParticipationRefundOrder.Status.SUCCEEDED:
                return refund, False
            try:
                payment = refund.payment_order.huifu_payment
            except ActivityHuifuPaymentOrder.DoesNotExist as exc:
                raise ValidationError("原支付单缺少汇付交易定位信息，禁止自动退款。") from exc
            refund.status = ActivityParticipationRefundOrder.Status.PROCESSING
            refund.failure_reason = ""
            refund.save(update_fields=("status", "failure_reason", "updated_at"))
        if not payment.gateway_merchant_id or not payment.req_date or not payment.req_seq_id:
            raise ValidationError("原支付单缺少汇付交易定位信息，禁止自动退款。")
        refund_request = _lock_or_create_refund_request(
            payment_kind=payment_kind,
            refund=refund,
        )
        refund_request.req_date = (
            refund_request.req_date or timezone.localtime(now).strftime("%Y%m%d")
        )
        refund_request.req_seq_id = refund_request.req_seq_id or refund.refund_no
        refund_request.gateway_merchant_id = payment.gateway_merchant_id
        should_submit = not (
            refund_request.gateway_response_digest
            or refund_request.gateway_status
            or refund_request.gateway_refund_no
            or refund_request.gateway_last_query_digest
        )
        refund_request.save()
        request_values = {
            "req_date": refund_request.req_date,
            "req_seq_id": refund_request.req_seq_id,
            "amount": refund.refund_amount,
            "org_req_date": payment.req_date,
            "org_req_seq_id": payment.req_seq_id,
            "org_hf_seq_id": payment.gateway_trade_no,
            "remark": refund.reason,
        }
        refund_request_id = refund_request.pk

    gateway = get_huifu_payment_gateway()
    if gateway.merchant_id != refund_request.gateway_merchant_id:
        raise HuifuGatewayError("退款商户配置与原支付单不一致。")
    fallback_hf_seq_id = refund_request.gateway_refund_no
    if should_submit:
        submitted = gateway.refund_payment(**request_values)
        if submitted.ord_amt and _amount_to_cents(submitted.ord_amt) != refund.refund_amount:
            raise HuifuGatewayError("退款受理金额与本地退款单不一致。")
        fallback_hf_seq_id = submitted.gateway_refund_no
        ActivityHuifuRefundOrder.objects.filter(pk=refund_request_id).update(
            gateway_refund_no=submitted.gateway_refund_no,
            gateway_status=submitted.trans_stat or "P",
            gateway_response_code=submitted.response_code,
            gateway_response_digest=submitted.response_digest,
            updated_at=timezone.now(),
        )

    refund_request = ActivityHuifuRefundOrder.objects.select_related(
        "publish_refund__publish_order__huifu_payment",
        "participation_refund__payment_order__huifu_payment",
    ).get(pk=refund_request_id)
    query = _query_refund(
        refund_request,
        fallback_hf_seq_id=fallback_hf_seq_id,
    )
    return _apply_refund_query_result(
        refund_request,
        query,
        fallback_hf_seq_id=fallback_hf_seq_id,
        now=now,
        raise_on_failure=True,
    )


def process_activity_huifu_publish_refund(refund_no: str, *, now=None):
    return _process_refund(
        payment_kind="activity_publish", refund_no=refund_no, now=now
    )


def process_activity_huifu_participation_refund(refund_no: str, *, now=None):
    return _process_refund(
        payment_kind="activity_participation", refund_no=refund_no, now=now
    )


def process_activity_huifu_refund_notification(
    *, fields: dict, payload_digest: str
) -> str:
    event_key = sha256(
        ":".join(
            (
                "activity-refund",
                fields["huifu_id"],
                fields["req_date"],
                fields["req_seq_id"],
                fields["hf_seq_id"],
                fields["trans_stat"],
            )
        ).encode("utf-8")
    ).hexdigest()
    with transaction.atomic():
        refund_request = (
            ActivityHuifuRefundOrder.objects.select_for_update(of=("self",))
            .select_related("publish_refund", "participation_refund")
            .filter(req_date=fields["req_date"], req_seq_id=fields["req_seq_id"])
            .first()
        )
        if refund_request is None:
            raise ValidationError({"notification": "退款通知未匹配到本地活动退款单。"})
        if refund_request.gateway_merchant_id != fields["huifu_id"]:
            raise ValidationError({"notification": "退款通知商户号与本地退款单不一致。"})
        _kind, refund, _payment = _refund_business(refund_request)
        if fields["ord_amt"] and _amount_to_cents(fields["ord_amt"]) != refund.refund_amount:
            raise ValidationError({"notification": "退款通知金额与本地退款单不一致。"})
        event, _ = ActivityHuifuNotification.objects.get_or_create(
            event_key=event_key,
            defaults={
                "refund": refund_request,
                "huifu_id": fields["huifu_id"],
                "req_date": fields["req_date"],
                "req_seq_id": fields["req_seq_id"],
                "hf_seq_id": fields["hf_seq_id"],
                "trans_type": fields["trans_type"],
                "notify_type": fields["notify_type"],
                "trans_stat": fields["trans_stat"],
                "amount": fields["ord_amt"],
                "payload_digest": payload_digest,
                "signature_verified": True,
            },
        )
        event = ActivityHuifuNotification.objects.select_for_update().get(pk=event.pk)
        if event.payload_digest != payload_digest or event.refund_id != refund_request.pk:
            raise ValidationError({"notification": "退款通知幂等键冲突。"})
        if event.status == ActivityHuifuNotification.Status.PROCESSED:
            return f"RECV_ORD_ID_{fields['req_seq_id']}"
        refund_request_id = refund_request.pk
        fallback_hf_seq_id = refund_request.gateway_refund_no or fields["hf_seq_id"]

    refund_request = ActivityHuifuRefundOrder.objects.select_related(
        "publish_refund__publish_order__huifu_payment",
        "participation_refund__payment_order__huifu_payment",
    ).get(pk=refund_request_id)
    query = _query_refund(
        refund_request,
        fallback_hf_seq_id=fallback_hf_seq_id,
    )
    _apply_refund_query_result(
        refund_request,
        query,
        fallback_hf_seq_id=fields["hf_seq_id"],
    )
    ActivityHuifuNotification.objects.filter(event_key=event_key).update(
        query_response_digest=query.response_digest,
        status=ActivityHuifuNotification.Status.PROCESSED,
        processed_at=timezone.now(),
    )
    return f"RECV_ORD_ID_{fields['req_seq_id']}"
