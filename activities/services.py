from datetime import timedelta

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from config.geospatial import gcj02_to_wgs84

from .models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivityRefundRecord,
    ActivityReport,
    ActivitySettlement,
)
from .payment_gateway import get_activity_payment_gateway
from .serializers import STANDARD_REFUND_SNAPSHOT


PARTICIPATION_PAYMENT_TTL = timedelta(minutes=30)
ACTIVITY_CONFIRMATION_PERIOD = timedelta(hours=24)
ACTIVITY_RISK_FREEZE_PERIOD = timedelta(days=7)


def create_activity_draft(*, organizer, validated_data) -> Activity:
    category = validated_data.pop("category_slug")
    longitude = validated_data.pop("longitude")
    latitude = validated_data.pop("latitude")
    validated_data.pop("refund_template_version")
    cover = validated_data.pop("cover")
    wgs84 = gcj02_to_wgs84(longitude, latitude)
    return Activity.objects.create(
        organizer=organizer,
        category=category,
        cover=cover,
        source_longitude=longitude,
        source_latitude=latitude,
        meeting_point=wgs84,
        refund_template_version="standard-v1",
        refund_rule_snapshot=STANDARD_REFUND_SNAPSHOT,
        status=Activity.Status.DRAFT,
        **validated_data,
    )


def _publish_order_no() -> str:
    return f"ACT{timezone.now():%Y%m%d%H%M%S%f}"


def calculate_publish_service_fee(principal_amount: int) -> int:
    """Calculate 10% in cents using explicit round-half-up semantics."""
    return (principal_amount + 5) // 10


def refund_publish_order(
    *,
    activity,
    publish_order,
    refund_type,
    reason,
    operator,
    principal_refund_amount=None,
    service_fee_refund_amount=None,
    retained_principal_destination="",
):
    if publish_order.status not in (
        ActivityPublishOrder.Status.PAID,
        ActivityPublishOrder.Status.PARTIALLY_REFUNDED,
        ActivityPublishOrder.Status.REFUNDED,
    ):
        raise ValidationError("活动发布支付单当前不可退款。")
    principal_refund_amount = (
        publish_order.aa_principal_amount
        if principal_refund_amount is None
        else principal_refund_amount
    )
    service_fee_refund_amount = (
        publish_order.platform_service_fee_amount
        if service_fee_refund_amount is None
        else service_fee_refund_amount
    )
    if not 0 <= principal_refund_amount <= publish_order.aa_principal_amount:
        raise ValidationError("发起人AA本金退款金额无效。")
    if not 0 <= service_fee_refund_amount <= publish_order.platform_service_fee_amount:
        raise ValidationError("发起人平台服务费退款金额无效。")
    refund_amount = principal_refund_amount + service_fee_refund_amount
    refund, created = ActivityRefundRecord.objects.get_or_create(
        publish_order=publish_order,
        defaults={
            "activity": activity,
            "beneficiary": publish_order.payer,
            "refund_type": refund_type,
            "principal_amount": principal_refund_amount,
            "service_fee_amount": service_fee_refund_amount,
            "refund_amount": refund_amount,
            "retained_principal_amount": (
                publish_order.aa_principal_amount - principal_refund_amount
            ),
            "retained_service_fee_amount": (
                publish_order.platform_service_fee_amount - service_fee_refund_amount
            ),
            "retained_principal_destination": retained_principal_destination,
            "reason": reason,
            "operator": operator,
            "refunded_at": timezone.now(),
        },
    )
    if created:
        publish_order.status = (
            ActivityPublishOrder.Status.REFUNDED
            if refund_amount == publish_order.payable_amount
            else ActivityPublishOrder.Status.PARTIALLY_REFUNDED
        )
        publish_order.save(update_fields=("status", "updated_at"))
    return refund


@transaction.atomic
def create_activity_report(*, activity_id, reporter, reason, description):
    activity = Activity.objects.filter(
        pk=activity_id,
        status__in=(
            Activity.Status.RECRUITING,
            Activity.Status.FORMED,
            Activity.Status.IN_PROGRESS,
            Activity.Status.COMPLETED,
            Activity.Status.CANCELLED,
            Activity.Status.FAILED_TO_FORM,
        ),
    ).first()
    if not activity:
        raise NotFound("活动不存在。")
    if activity.organizer_id == reporter.pk:
        raise PermissionDenied("不能举报自己发起的活动。")
    existing = ActivityReport.objects.filter(
        activity=activity,
        reporter=reporter,
        status__in=(ActivityReport.Status.PENDING, ActivityReport.Status.PROCESSING),
    ).first()
    if existing:
        return existing, False
    return ActivityReport.objects.create(
        activity=activity,
        reporter=reporter,
        reason=reason,
        description=description.strip(),
    ), True


@transaction.atomic
def get_or_create_publish_order(*, activity_id: int, user):
    activity = Activity.objects.select_for_update().filter(pk=activity_id, organizer=user).first()
    if not activity:
        raise NotFound("活动草稿不存在。")
    if activity.status != Activity.Status.DRAFT:
        raise ValidationError("当前活动不需要重复支付发布费用。")
    existing = ActivityPublishOrder.objects.filter(
        activity=activity,
        payer=user,
        status=ActivityPublishOrder.Status.PENDING_PAYMENT,
    ).first()
    if existing:
        return existing
    service_fee = calculate_publish_service_fee(activity.aa_principal_amount)
    return ActivityPublishOrder.objects.create(
        activity=activity,
        order_no=_publish_order_no(),
        payer=user,
        aa_principal_amount=activity.aa_principal_amount,
        platform_service_fee_amount=service_fee,
        payable_amount=activity.aa_principal_amount + service_fee,
        pricing_snapshot={"platform_service_fee_rate": "0.10", "rounding": "half_up"},
    )


@transaction.atomic
def simulate_publish_payment(*, activity_id: int, user):
    activity = Activity.objects.select_for_update().filter(pk=activity_id, organizer=user).first()
    if not activity:
        raise NotFound("活动草稿不存在。")
    order = ActivityPublishOrder.objects.select_for_update().filter(
        activity=activity,
        payer=user,
        status=ActivityPublishOrder.Status.PENDING_PAYMENT,
    ).first()
    if not order:
        raise ValidationError("发布支付单不在待支付状态。")
    order.status = ActivityPublishOrder.Status.PAID
    order.paid_at = timezone.now()
    order.save(update_fields=("status", "paid_at", "updated_at"))
    activity.status = Activity.Status.PENDING_REVIEW
    activity.save(update_fields=("status", "updated_at"))
    return order


def _active_count(activity: Activity) -> int:
    return ActivityParticipation.objects.filter(
        activity=activity,
        status=ActivityParticipation.Status.ACTIVE,
    ).count()


def _occupied_count(activity: Activity, *, now=None) -> int:
    now = now or timezone.now()
    return ActivityParticipation.objects.filter(activity=activity).filter(
        Q(status=ActivityParticipation.Status.ACTIVE)
        | Q(
            status=ActivityParticipation.Status.PENDING_PAYMENT,
            payment_expires_at__gt=now,
        )
    ).count()


def sync_activity_formation_status(activity: Activity, participant_count: int) -> None:
    if activity.status not in (Activity.Status.RECRUITING, Activity.Status.FORMED):
        return
    target = activity.status
    if participant_count >= activity.min_participants:
        target = Activity.Status.FORMED
    elif activity.status == Activity.Status.FORMED and activity.formation_deadline > timezone.now():
        target = Activity.Status.RECRUITING
    if target != activity.status:
        activity.status = target
        activity.save(update_fields=("status", "updated_at"))


def _close_pending_payment_order(order, *, now):
    if order.status != ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT:
        return
    order.status = ActivityParticipationPaymentOrder.Status.CLOSED
    order.closed_at = now
    order.save(update_fields=("status", "closed_at", "updated_at"))


def expire_pending_participation_orders(*, activity=None, now=None) -> int:
    now = now or timezone.now()
    queryset = ActivityParticipationPaymentOrder.objects.select_for_update().filter(
        status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT,
        expires_at__lte=now,
    )
    if activity is not None:
        queryset = queryset.filter(participation__activity=activity)
    expired = 0
    for order in queryset.select_related("participation"):
        _close_pending_payment_order(order, now=now)
        participation = order.participation
        if participation.status == ActivityParticipation.Status.PENDING_PAYMENT:
            participation.status = ActivityParticipation.Status.EXPIRED
            participation.payment_expires_at = None
            participation.save(
                update_fields=("status", "payment_expires_at", "updated_at")
            )
        expired += 1
    return expired


def _validate_participation_eligibility(activity, user, now):
    if activity.organizer_id == user.pk:
        raise PermissionDenied("组织者无需重复报名自己的活动。")
    if user.verification_status != user.VerificationStatus.VERIFIED:
        raise PermissionDenied("完成实名认证后才能报名收费活动。")
    if user.account_status != user.AccountStatus.ACTIVE:
        raise PermissionDenied("当前账号状态不可报名活动。")
    if activity.status not in (Activity.Status.RECRUITING, Activity.Status.FORMED):
        raise ValidationError("当前活动不可报名。")
    if activity.formation_deadline <= now:
        raise ValidationError("活动报名已截止。")


@transaction.atomic
def get_or_create_participation_order(*, activity_id: int, user, channel: str):
    try:
        activity = Activity.objects.select_for_update().get(pk=activity_id)
    except Activity.DoesNotExist as exc:
        raise NotFound("活动不存在。") from exc
    now = timezone.now()
    _validate_participation_eligibility(activity, user, now)
    expire_pending_participation_orders(activity=activity, now=now)

    participation = ActivityParticipation.objects.select_for_update().filter(
        activity=activity, user=user
    ).first()
    if participation and participation.status == ActivityParticipation.Status.ACTIVE:
        order = participation.payment_orders.filter(
            status__in=(
                ActivityParticipationPaymentOrder.Status.PAID,
                ActivityParticipationPaymentOrder.Status.PARTIALLY_REFUNDED,
            )
        ).first()
        if not order:
            raise ValidationError("报名记录缺少有效支付单，请联系客服处理。")
        return participation, order, False

    if participation:
        order = participation.payment_orders.filter(
            status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT,
            expires_at__gt=now,
        ).first()
        if order:
            if order.channel != channel:
                order.channel = channel
                order.save(update_fields=("channel", "updated_at"))
            return participation, order, False

    if _occupied_count(activity, now=now) >= activity.capacity:
        raise ValidationError("活动名额已满。")

    service_fee = calculate_publish_service_fee(activity.aa_principal_amount)
    expires_at = now + PARTICIPATION_PAYMENT_TTL
    snapshot = {
        "platform_service_fee_rate": "0.10",
        "rounding": "half_up",
        "refund_template_version": activity.refund_template_version,
    }
    if participation:
        participation.status = ActivityParticipation.Status.PENDING_PAYMENT
        participation.aa_principal_amount = activity.aa_principal_amount
        participation.platform_service_fee_amount = service_fee
        participation.payable_amount = activity.aa_principal_amount + service_fee
        participation.pricing_snapshot = snapshot
        participation.refund_rule_snapshot = activity.refund_rule_snapshot
        participation.rule_confirmed_at = now
        participation.payment_expires_at = expires_at
        participation.joined_at = None
        participation.cancelled_at = None
        participation.cancellation_reason = ""
        participation.cancelled_by_role = ""
        participation.save(update_fields=(
            "status", "aa_principal_amount", "platform_service_fee_amount",
            "payable_amount", "pricing_snapshot", "refund_rule_snapshot",
            "rule_confirmed_at", "payment_expires_at", "joined_at",
            "cancelled_at", "cancellation_reason", "cancelled_by_role", "updated_at",
        ))
    else:
        participation = ActivityParticipation.objects.create(
            activity=activity,
            user=user,
            status=ActivityParticipation.Status.PENDING_PAYMENT,
            aa_principal_amount=activity.aa_principal_amount,
            platform_service_fee_amount=service_fee,
            payable_amount=activity.aa_principal_amount + service_fee,
            pricing_snapshot=snapshot,
            refund_rule_snapshot=activity.refund_rule_snapshot,
            rule_confirmed_at=now,
            payment_expires_at=expires_at,
        )
    order = ActivityParticipationPaymentOrder.objects.create(
        participation=participation,
        payer=user,
        aa_principal_amount=participation.aa_principal_amount,
        platform_service_fee_amount=participation.platform_service_fee_amount,
        payable_amount=participation.payable_amount,
        pricing_snapshot=participation.pricing_snapshot,
        channel=channel,
        expires_at=expires_at,
    )
    return participation, order, True


@transaction.atomic
def simulate_participation_payment(*, activity_id: int, user):
    try:
        activity = Activity.objects.select_for_update().get(pk=activity_id)
    except Activity.DoesNotExist as exc:
        raise NotFound("活动不存在。") from exc
    participation = ActivityParticipation.objects.select_for_update().filter(
        activity=activity, user=user
    ).first()
    if not participation:
        raise ValidationError("请先创建报名支付单。")
    paid_order = participation.payment_orders.filter(
        status__in=(
            ActivityParticipationPaymentOrder.Status.PAID,
            ActivityParticipationPaymentOrder.Status.PARTIALLY_REFUNDED,
        )
    ).first()
    if participation.status == ActivityParticipation.Status.ACTIVE and paid_order:
        return participation, paid_order, False
    order = participation.payment_orders.select_for_update().filter(
        status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT
    ).first()
    if not order:
        raise ValidationError("报名支付单不在待支付状态。")
    now = timezone.now()
    if order.expires_at <= now:
        _close_pending_payment_order(order, now=now)
        participation.status = ActivityParticipation.Status.EXPIRED
        participation.payment_expires_at = None
        participation.save(update_fields=("status", "payment_expires_at", "updated_at"))
        raise ValidationError("报名支付已超时，名额已释放，请重新报名。")
    _validate_participation_eligibility(activity, user, now)
    result = get_activity_payment_gateway(order.channel).confirm_payment(
        order_no=order.order_no, amount=order.payable_amount
    )
    order.status = ActivityParticipationPaymentOrder.Status.PAID
    order.gateway_trade_no = result.gateway_trade_no
    order.paid_at = now
    order.save(update_fields=("status", "gateway_trade_no", "paid_at", "updated_at"))
    participation.status = ActivityParticipation.Status.ACTIVE
    participation.joined_at = now
    participation.payment_expires_at = None
    participation.save(
        update_fields=("status", "joined_at", "payment_expires_at", "updated_at")
    )
    participant_count = _active_count(activity)
    sync_activity_formation_status(activity, participant_count)
    return participation, order, True


def _refunded_totals(payment_order):
    totals = payment_order.refund_orders.filter(
        status=ActivityParticipationRefundOrder.Status.SUCCEEDED
    ).aggregate(
        principal=Sum("principal_refund_amount"),
        service_fee=Sum("service_fee_refund_amount"),
        total=Sum("refund_amount"),
    )
    return (
        totals["principal"] or 0,
        totals["service_fee"] or 0,
        totals["total"] or 0,
    )


def _round_percent(amount: int, percent: int) -> int:
    return (amount * percent + 50) // 100


def calculate_participant_cancellation_refund(*, participation, now=None):
    now = now or timezone.now()
    hours_before = (participation.activity.starts_at - now).total_seconds() / 3600
    if hours_before < 0:
        raise ValidationError("活动已经开始，无法直接取消报名，请申请售后。")
    snapshot = participation.refund_rule_snapshot or participation.activity.refund_rule_snapshot
    rules = snapshot.get("rules", STANDARD_REFUND_SNAPSHOT["rules"])
    matched = None
    for rule in sorted(rules, key=lambda item: item.get("before_hours", 0), reverse=True):
        if hours_before >= rule.get("before_hours", 0):
            matched = rule
            break
    matched = matched or {"principal_refund_percent": 0, "service_fee_refund_percent": 0}
    principal_refund = _round_percent(
        participation.aa_principal_amount,
        int(matched.get("principal_refund_percent", 0)),
    )
    service_fee_refund = _round_percent(
        participation.platform_service_fee_amount,
        int(matched.get("service_fee_refund_percent", 0)),
    )
    retained_principal = participation.aa_principal_amount - principal_refund
    return {
        "principal_refund_amount": principal_refund,
        "service_fee_refund_amount": service_fee_refund,
        "retained_principal_amount": retained_principal,
        "retained_service_fee_amount": (
            participation.platform_service_fee_amount - service_fee_refund
        ),
        "retained_principal_destination": (
            ActivityParticipationRefundOrder.PrincipalDestination.ORGANIZER
            if retained_principal
            else ActivityParticipationRefundOrder.PrincipalDestination.NONE
        ),
    }


def create_and_process_participation_refund(
    *,
    participation,
    payment_order,
    refund_type,
    idempotency_key,
    principal_refund_amount,
    service_fee_refund_amount,
    retained_principal_amount=0,
    retained_service_fee_amount=0,
    retained_principal_destination=ActivityParticipationRefundOrder.PrincipalDestination.NONE,
    reason,
    operator=None,
):
    existing = ActivityParticipationRefundOrder.objects.filter(
        idempotency_key=idempotency_key
    ).first()
    if existing:
        return existing, False
    refunded_principal, refunded_service_fee, _ = _refunded_totals(payment_order)
    if principal_refund_amount > payment_order.aa_principal_amount - refunded_principal:
        raise ValidationError("AA本金退款金额超过可退金额。")
    if (
        service_fee_refund_amount
        > payment_order.platform_service_fee_amount - refunded_service_fee
    ):
        raise ValidationError("平台服务费退款金额超过可退金额。")
    refund = ActivityParticipationRefundOrder.objects.create(
        idempotency_key=idempotency_key,
        activity=participation.activity,
        participation=participation,
        payment_order=payment_order,
        beneficiary=participation.user,
        refund_type=refund_type,
        principal_refund_amount=principal_refund_amount,
        service_fee_refund_amount=service_fee_refund_amount,
        refund_amount=principal_refund_amount + service_fee_refund_amount,
        retained_principal_amount=retained_principal_amount,
        retained_service_fee_amount=retained_service_fee_amount,
        retained_principal_destination=retained_principal_destination,
        status=ActivityParticipationRefundOrder.Status.PROCESSING,
        reason=reason,
        operator=operator,
    )
    result = get_activity_payment_gateway(payment_order.channel).refund(
        refund_no=refund.refund_no, amount=refund.refund_amount
    )
    refund.status = ActivityParticipationRefundOrder.Status.SUCCEEDED
    refund.gateway_refund_no = result.gateway_refund_no
    refund.refunded_at = timezone.now()
    refund.save(
        update_fields=("status", "gateway_refund_no", "refunded_at", "updated_at")
    )
    _, _, refunded_total = _refunded_totals(payment_order)
    if refunded_total >= payment_order.payable_amount:
        payment_order.status = ActivityParticipationPaymentOrder.Status.REFUNDED
    elif refunded_total:
        payment_order.status = ActivityParticipationPaymentOrder.Status.PARTIALLY_REFUNDED
    payment_order.save(update_fields=("status", "updated_at"))
    return refund, True


def _latest_refundable_payment_order(participation):
    return participation.payment_orders.select_for_update().filter(
        status__in=(
            ActivityParticipationPaymentOrder.Status.PAID,
            ActivityParticipationPaymentOrder.Status.PARTIALLY_REFUNDED,
        )
    ).first()


@transaction.atomic
def cancel_activity_participation(activity_id: int, user, reason: str = "用户主动取消报名"):
    try:
        activity = Activity.objects.select_for_update().get(pk=activity_id)
    except Activity.DoesNotExist as exc:
        raise NotFound("活动不存在。") from exc
    participation = ActivityParticipation.objects.select_for_update().filter(
        activity=activity, user=user
    ).first()
    if not participation:
        raise ValidationError("你尚未报名该活动。")
    if participation.status == ActivityParticipation.Status.CANCELLED:
        return participation, participation.refund_orders.first(), False
    now = timezone.now()
    if participation.status == ActivityParticipation.Status.PENDING_PAYMENT:
        pending_order = participation.payment_orders.select_for_update().filter(
            status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT
        ).first()
        if pending_order:
            _close_pending_payment_order(pending_order, now=now)
        participation.status = ActivityParticipation.Status.CANCELLED
        participation.cancelled_at = now
        participation.cancellation_reason = reason
        participation.cancelled_by_role = ActivityParticipation.CancelledByRole.USER
        participation.payment_expires_at = None
        participation.save(update_fields=(
            "status", "cancelled_at", "cancellation_reason", "cancelled_by_role",
            "payment_expires_at", "updated_at",
        ))
        return participation, None, True
    if participation.status != ActivityParticipation.Status.ACTIVE:
        raise ValidationError("当前报名状态不能取消。")
    if participation.after_sales_cases.filter(
        status__in=(
            ActivityAfterSalesCase.Status.PENDING,
            ActivityAfterSalesCase.Status.PROCESSING,
        )
    ).exists():
        raise ValidationError("该报名已有售后在处理中，请勿重复取消。")
    payment_order = _latest_refundable_payment_order(participation)
    if not payment_order:
        raise ValidationError("报名支付单缺失，请联系客服处理。")
    amounts = calculate_participant_cancellation_refund(
        participation=participation, now=now
    )
    refund, _ = create_and_process_participation_refund(
        participation=participation,
        payment_order=payment_order,
        refund_type=ActivityParticipationRefundOrder.RefundType.PARTICIPANT_CANCELLATION,
        idempotency_key=f"participant-cancel:{participation.pk}:{payment_order.pk}",
        reason=reason,
        **amounts,
    )
    participation.status = ActivityParticipation.Status.CANCELLED
    participation.cancelled_at = now
    participation.cancellation_reason = reason
    participation.cancelled_by_role = ActivityParticipation.CancelledByRole.USER
    participation.save(update_fields=(
        "status", "cancelled_at", "cancellation_reason", "cancelled_by_role", "updated_at",
    ))
    participant_count = _active_count(activity)
    sync_activity_formation_status(activity, participant_count)
    return participation, refund, True


@transaction.atomic
def create_activity_after_sales_case(
    *, activity_id: int, applicant, reason: str, description: str, evidence_object_keys=None
):
    participation = ActivityParticipation.objects.select_for_update().select_related(
        "activity", "user"
    ).filter(activity_id=activity_id, user=applicant).first()
    if not participation:
        raise NotFound("未找到该活动的报名记录。")
    existing = participation.after_sales_cases.filter(
        status__in=(
            ActivityAfterSalesCase.Status.PENDING,
            ActivityAfterSalesCase.Status.PROCESSING,
        )
    ).first()
    if existing:
        return existing, False
    settlement = ActivitySettlement.objects.select_for_update().filter(
        activity=participation.activity
    ).first()
    if settlement and settlement.status == ActivitySettlement.Status.SETTLED:
        raise ValidationError("活动资金已经结算，当前不能再发起退款售后。")
    payment_order = _latest_refundable_payment_order(participation)
    if not payment_order:
        raise ValidationError("当前报名没有可申请退款的支付金额。")
    refunded_principal, refunded_service_fee, _ = _refunded_totals(payment_order)
    requested_principal = payment_order.aa_principal_amount - refunded_principal
    requested_service_fee = (
        payment_order.platform_service_fee_amount - refunded_service_fee
    )
    if requested_principal + requested_service_fee <= 0:
        raise ValidationError("该报名已无可退金额。")
    case = ActivityAfterSalesCase.objects.create(
        participation=participation,
        applicant=applicant,
        reason=reason,
        description=description.strip(),
        evidence_object_keys=evidence_object_keys or [],
        requested_principal_amount=requested_principal,
        requested_service_fee_amount=requested_service_fee,
        requested_amount=requested_principal + requested_service_fee,
    )
    freeze_activity_settlement_for_after_sales(
        activity=participation.activity,
        reason=f"售后单 {case.case_no} 待处理",
    )
    return case, True


def refund_all_activity_participations(
    *, activity, refund_type, reason, cancelled_by_role, operator=None
):
    now = timezone.now()
    participations = list(
        ActivityParticipation.objects.select_for_update()
        .filter(
            activity=activity,
            status__in=(
                ActivityParticipation.Status.PENDING_PAYMENT,
                ActivityParticipation.Status.ACTIVE,
            ),
        )
        .select_related("user")
    )
    refundable_orders = {}
    for participation in participations:
        if participation.status == ActivityParticipation.Status.ACTIVE:
            payment_order = _latest_refundable_payment_order(participation)
            if not payment_order:
                raise ValidationError(
                    f"参与者 {participation.user.nickname} 缺少可退款支付单，操作已阻止。"
                )
            refundable_orders[participation.pk] = payment_order
    refunds = []
    for participation in participations:
        if participation.status == ActivityParticipation.Status.PENDING_PAYMENT:
            pending_order = participation.payment_orders.select_for_update().filter(
                status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT
            ).first()
            if pending_order:
                _close_pending_payment_order(pending_order, now=now)
        else:
            payment_order = refundable_orders[participation.pk]
            refunded_principal, refunded_service_fee, _ = _refunded_totals(payment_order)
            refund, _ = create_and_process_participation_refund(
                participation=participation,
                payment_order=payment_order,
                refund_type=refund_type,
                idempotency_key=f"{refund_type}:{activity.pk}:{participation.pk}:{payment_order.pk}",
                principal_refund_amount=(
                    payment_order.aa_principal_amount - refunded_principal
                ),
                service_fee_refund_amount=(
                    payment_order.platform_service_fee_amount - refunded_service_fee
                ),
                reason=reason,
                operator=operator,
            )
            refunds.append(refund)
        participation.status = ActivityParticipation.Status.CANCELLED
        participation.cancelled_at = now
        participation.cancellation_reason = reason
        participation.cancelled_by_role = cancelled_by_role
        participation.payment_expires_at = None
        participation.save(update_fields=(
            "status", "cancelled_at", "cancellation_reason", "cancelled_by_role",
            "payment_expires_at", "updated_at",
        ))
    return refunds


@transaction.atomic
def cancel_activity_by_organizer(*, activity_id: int, organizer, reason: str):
    activity = Activity.objects.select_for_update().filter(
        pk=activity_id, organizer=organizer
    ).first()
    if not activity:
        raise NotFound("活动不存在。")
    if activity.status not in (Activity.Status.RECRUITING, Activity.Status.FORMED):
        raise ValidationError("仅报名中或已成局活动可以取消。")
    now = timezone.now()
    if activity.starts_at <= now:
        raise ValidationError("活动已经开始，请联系客服处理。")
    publish_order = ActivityPublishOrder.objects.select_for_update().filter(
        activity=activity,
        status__in=(
            ActivityPublishOrder.Status.PAID,
            ActivityPublishOrder.Status.PARTIALLY_REFUNDED,
        ),
    ).order_by("-created_at").first()
    if not publish_order:
        raise ValidationError("活动发布支付单不在可退款状态。")
    refund_all_activity_participations(
        activity=activity,
        refund_type=ActivityParticipationRefundOrder.RefundType.ORGANIZER_CANCELLATION,
        reason=reason,
        cancelled_by_role=ActivityParticipation.CancelledByRole.ORGANIZER,
        operator=organizer,
    )
    published_at = activity.published_at or activity.created_at
    hours_since_publish = (now - published_at).total_seconds() / 3600
    hours_before_start = (activity.starts_at - now).total_seconds() / 3600
    if hours_since_publish <= 12:
        principal_refund = publish_order.aa_principal_amount
        fee_refund = publish_order.platform_service_fee_amount
        destination = ""
    elif hours_before_start >= 6:
        principal_refund = publish_order.aa_principal_amount
        fee_refund = 0
        destination = ""
    else:
        principal_refund = _round_percent(publish_order.aa_principal_amount, 70)
        fee_refund = 0
        destination = "platform"
    publish_refund = refund_publish_order(
        activity=activity,
        publish_order=publish_order,
        refund_type=ActivityRefundRecord.RefundType.ORGANIZER_CANCELLATION,
        reason=reason,
        operator=organizer,
        principal_refund_amount=principal_refund,
        service_fee_refund_amount=fee_refund,
        retained_principal_destination=destination,
    )
    activity.status = Activity.Status.CANCELLED
    activity.cancellation_reason = reason
    activity.cancelled_by = organizer
    activity.cancelled_at = now
    activity.save(update_fields=(
        "status", "cancellation_reason", "cancelled_by", "cancelled_at", "updated_at",
    ))
    return activity, publish_refund


def calculate_activity_settlement_amounts(activity: Activity) -> dict:
    publish_order = activity.publish_orders.filter(
        status__in=(
            ActivityPublishOrder.Status.PAID,
            ActivityPublishOrder.Status.PARTIALLY_REFUNDED,
            ActivityPublishOrder.Status.REFUNDED,
        )
    ).order_by("-created_at", "-id").first()
    if not publish_order:
        raise ValidationError("已完成活动缺少有效发布支付单，无法生成结算。")
    publish_refund = ActivityRefundRecord.objects.filter(
        publish_order=publish_order
    ).first()
    organizer_principal = max(
        0,
        publish_order.aa_principal_amount
        - (publish_refund.principal_amount if publish_refund else 0),
    )
    platform_service_fee = max(
        0,
        publish_order.platform_service_fee_amount
        - (publish_refund.service_fee_amount if publish_refund else 0),
    )
    participant_principal = 0
    retained_participant_principal = 0
    unallocated_principal = 0
    payment_snapshots = []
    participations = ActivityParticipation.objects.filter(
        activity=activity
    ).prefetch_related("payment_orders__refund_orders")
    paid_statuses = (
        ActivityParticipationPaymentOrder.Status.PAID,
        ActivityParticipationPaymentOrder.Status.PARTIALLY_REFUNDED,
        ActivityParticipationPaymentOrder.Status.REFUNDED,
    )
    for participation in participations:
        paid_orders = [
            order
            for order in participation.payment_orders.all()
            if order.status in paid_statuses and order.paid_at is not None
        ]
        current_order_id = paid_orders[0].pk if participation.status == ActivityParticipation.Status.ACTIVE and paid_orders else None
        for order in paid_orders:
            succeeded_refunds = [
                refund for refund in order.refund_orders.all()
                if refund.status == ActivityParticipationRefundOrder.Status.SUCCEEDED
            ]
            refunded_principal = sum(
                refund.principal_refund_amount for refund in succeeded_refunds
            )
            refunded_service_fee = sum(
                refund.service_fee_refund_amount for refund in succeeded_refunds
            )
            net_principal = max(0, order.aa_principal_amount - refunded_principal)
            net_service_fee = max(
                0, order.platform_service_fee_amount - refunded_service_fee
            )
            platform_service_fee += net_service_fee
            organizer_retained_declared = sum(
                refund.retained_principal_amount
                for refund in succeeded_refunds
                if refund.retained_principal_destination
                == ActivityParticipationRefundOrder.PrincipalDestination.ORGANIZER
            )
            if order.pk == current_order_id:
                participant_principal += net_principal
                allocation = "active_participant"
            else:
                retained = min(net_principal, organizer_retained_declared)
                retained_participant_principal += retained
                unallocated_principal += max(0, net_principal - retained)
                allocation = "organizer_retained" if retained else "unallocated"
            payment_snapshots.append({
                "order_no": order.order_no,
                "participation_id": participation.pk,
                "participation_status": participation.status,
                "net_principal_amount": net_principal,
                "net_service_fee_amount": net_service_fee,
                "allocation": allocation,
                "refund_nos": [refund.refund_no for refund in succeeded_refunds],
            })
    settlement_amount = (
        organizer_principal
        + participant_principal
        + retained_participant_principal
    )
    return {
        "organizer_principal_amount": organizer_principal,
        "participant_principal_amount": participant_principal,
        "retained_participant_principal_amount": retained_participant_principal,
        "settlement_amount": settlement_amount,
        "platform_service_fee_amount": platform_service_fee,
        "calculation_snapshot": {
            "version": "activity-settlement-v1",
            "publish_order_no": publish_order.order_no,
            "publish_refund_no": publish_refund.refund_no if publish_refund else None,
            "payment_orders": payment_snapshots,
            "unallocated_principal_amount": unallocated_principal,
        },
    }


def _apply_settlement_calculation(settlement, amounts):
    for field in (
        "organizer_principal_amount",
        "participant_principal_amount",
        "retained_participant_principal_amount",
        "settlement_amount",
        "platform_service_fee_amount",
        "calculation_snapshot",
    ):
        setattr(settlement, field, amounts[field])


def _has_open_activity_after_sales(activity) -> bool:
    return ActivityAfterSalesCase.objects.filter(
        participation__activity=activity,
        status__in=(
            ActivityAfterSalesCase.Status.PENDING,
            ActivityAfterSalesCase.Status.PROCESSING,
        ),
    ).exists()


@transaction.atomic
def ensure_activity_settlement(*, activity_id: int, now=None):
    now = now or timezone.now()
    activity = Activity.objects.select_for_update().select_related("organizer").filter(
        pk=activity_id
    ).first()
    if not activity:
        raise NotFound("活动不存在。")
    if activity.status != Activity.Status.COMPLETED:
        raise ValidationError("仅已完成活动可以生成结算单。")
    amounts = calculate_activity_settlement_amounts(activity)
    settlement = ActivitySettlement.objects.select_for_update().filter(
        activity=activity
    ).first()
    if settlement:
        if settlement.status != ActivitySettlement.Status.SETTLED:
            _apply_settlement_calculation(settlement, amounts)
            settlement.save(update_fields=(
                "organizer_principal_amount", "participant_principal_amount",
                "retained_participant_principal_amount", "settlement_amount",
                "platform_service_fee_amount", "calculation_snapshot", "updated_at",
            ))
        return settlement, False
    confirmation_started_at = activity.ends_at
    confirmation_deadline = confirmation_started_at + ACTIVITY_CONFIRMATION_PERIOD
    freeze_until = confirmation_deadline + ACTIVITY_RISK_FREEZE_PERIOD
    open_after_sales = _has_open_activity_after_sales(activity)
    settlement = ActivitySettlement.objects.create(
        activity=activity,
        beneficiary=activity.organizer,
        confirmation_started_at=confirmation_started_at,
        confirmation_deadline=confirmation_deadline,
        freeze_until=freeze_until,
        status=(
            ActivitySettlement.Status.DISPUTE_FROZEN
            if open_after_sales else ActivitySettlement.Status.CONFIRMING
        ),
        dispute_source=(
            ActivitySettlement.DisputeSource.AFTER_SALES if open_after_sales else ""
        ),
        dispute_reason=("存在待处理活动退款/售后" if open_after_sales else ""),
        **amounts,
    )
    return settlement, True


@transaction.atomic
def freeze_activity_settlement_for_after_sales(*, activity, reason: str):
    settlement = ActivitySettlement.objects.select_for_update().filter(
        activity=activity
    ).first()
    if not settlement and activity.status == Activity.Status.COMPLETED:
        settlement, _ = ensure_activity_settlement(activity_id=activity.pk)
    if not settlement:
        return None, False
    if settlement.status == ActivitySettlement.Status.SETTLED:
        raise ValidationError("活动资金已经结算，当前不能再发起退款售后。")
    if settlement.dispute_source == ActivitySettlement.DisputeSource.ADMIN:
        return settlement, False
    changed = settlement.status != ActivitySettlement.Status.DISPUTE_FROZEN
    settlement.status = ActivitySettlement.Status.DISPUTE_FROZEN
    settlement.dispute_source = ActivitySettlement.DisputeSource.AFTER_SALES
    settlement.dispute_reason = reason.strip()
    settlement.save(update_fields=(
        "status", "dispute_source", "dispute_reason", "updated_at",
    ))
    return settlement, changed


@transaction.atomic
def release_activity_settlement_after_sales(*, activity, now=None):
    now = now or timezone.now()
    settlement = ActivitySettlement.objects.select_for_update().filter(
        activity=activity
    ).first()
    if (
        not settlement
        or settlement.status == ActivitySettlement.Status.SETTLED
        or settlement.dispute_source != ActivitySettlement.DisputeSource.AFTER_SALES
        or _has_open_activity_after_sales(activity)
    ):
        return settlement, False
    amounts = calculate_activity_settlement_amounts(activity)
    _apply_settlement_calculation(settlement, amounts)
    if now < settlement.confirmation_deadline:
        settlement.status = ActivitySettlement.Status.CONFIRMING
        settlement.risk_frozen_at = None
    else:
        settlement.status = ActivitySettlement.Status.RISK_FROZEN
        settlement.risk_frozen_at = settlement.confirmation_deadline
    settlement.dispute_source = ""
    settlement.dispute_reason = ""
    settlement.save(update_fields=(
        "status", "risk_frozen_at", "dispute_source", "dispute_reason",
        "organizer_principal_amount", "participant_principal_amount",
        "retained_participant_principal_amount", "settlement_amount",
        "platform_service_fee_amount", "calculation_snapshot", "updated_at",
    ))
    return settlement, True


@transaction.atomic
def advance_activity_settlement(*, settlement_id: int, now=None):
    now = now or timezone.now()
    settlement = ActivitySettlement.objects.select_for_update().select_related(
        "activity"
    ).get(pk=settlement_id)
    if settlement.status == ActivitySettlement.Status.SETTLED:
        return settlement, False
    activity = settlement.activity
    if _has_open_activity_after_sales(activity):
        if settlement.dispute_source != ActivitySettlement.DisputeSource.ADMIN:
            settlement.status = ActivitySettlement.Status.DISPUTE_FROZEN
            settlement.dispute_source = ActivitySettlement.DisputeSource.AFTER_SALES
            settlement.dispute_reason = "存在待处理活动退款/售后"
            settlement.save(update_fields=(
                "status", "dispute_source", "dispute_reason", "updated_at",
            ))
        return settlement, False
    if settlement.dispute_source == ActivitySettlement.DisputeSource.ADMIN:
        return settlement, False
    if settlement.dispute_source == ActivitySettlement.DisputeSource.AFTER_SALES:
        release_activity_settlement_after_sales(activity=activity, now=now)
        settlement.refresh_from_db()
    amounts = calculate_activity_settlement_amounts(activity)
    _apply_settlement_calculation(settlement, amounts)
    changed = False
    if (
        settlement.status == ActivitySettlement.Status.CONFIRMING
        and now >= settlement.confirmation_deadline
    ):
        settlement.status = ActivitySettlement.Status.RISK_FROZEN
        settlement.risk_frozen_at = settlement.confirmation_deadline
        changed = True
    if (
        settlement.status == ActivitySettlement.Status.RISK_FROZEN
        and now >= settlement.freeze_until
    ):
        settlement.status = ActivitySettlement.Status.SETTLED
        settlement.settled_at = now
        changed = True
    settlement.save(update_fields=(
        "status", "risk_frozen_at", "settled_at",
        "organizer_principal_amount", "participant_principal_amount",
        "retained_participant_principal_amount", "settlement_amount",
        "platform_service_fee_amount", "calculation_snapshot", "updated_at",
    ))
    return settlement, changed


@transaction.atomic
def fail_unformed_activity(*, activity_id: int, now=None):
    now = now or timezone.now()
    activity = Activity.objects.select_for_update().filter(pk=activity_id).first()
    if not activity:
        raise NotFound("活动不存在。")
    if activity.status != Activity.Status.RECRUITING:
        return activity, False
    if activity.formation_deadline > now:
        return activity, False
    if _active_count(activity) >= activity.min_participants:
        activity.status = Activity.Status.FORMED
        activity.save(update_fields=("status", "updated_at"))
        return activity, True
    publish_order = ActivityPublishOrder.objects.select_for_update().filter(
        activity=activity,
        status__in=(
            ActivityPublishOrder.Status.PAID,
            ActivityPublishOrder.Status.PARTIALLY_REFUNDED,
        ),
    ).order_by("-created_at").first()
    if not publish_order:
        raise ValidationError("未成局活动缺少可退款发布支付单。")
    reason = "达到成局截止时间仍未满足最少成局人数"
    refund_all_activity_participations(
        activity=activity,
        refund_type=ActivityParticipationRefundOrder.RefundType.FAILED_TO_FORM,
        reason=reason,
        cancelled_by_role=ActivityParticipation.CancelledByRole.PLATFORM,
    )
    refund_publish_order(
        activity=activity,
        publish_order=publish_order,
        refund_type=ActivityRefundRecord.RefundType.FAILED_TO_FORM,
        reason=reason,
        operator=None,
    )
    activity.status = Activity.Status.FAILED_TO_FORM
    activity.cancellation_reason = reason
    activity.cancelled_at = now
    activity.save(update_fields=(
        "status", "cancellation_reason", "cancelled_at", "updated_at",
    ))
    return activity, True


@transaction.atomic
def start_formed_activity(*, activity_id: int, now=None):
    now = now or timezone.now()
    activity = Activity.objects.select_for_update().filter(pk=activity_id).first()
    if not activity:
        raise NotFound("活动不存在。")
    if activity.status != Activity.Status.FORMED or activity.starts_at > now:
        return activity, False
    activity.status = Activity.Status.IN_PROGRESS
    activity.save(update_fields=("status", "updated_at"))
    return activity, True


@transaction.atomic
def complete_started_activity(*, activity_id: int, now=None):
    now = now or timezone.now()
    activity = Activity.objects.select_for_update().filter(pk=activity_id).first()
    if not activity:
        raise NotFound("活动不存在。")
    if activity.status != Activity.Status.IN_PROGRESS or activity.ends_at > now:
        return activity, False
    activity.status = Activity.Status.COMPLETED
    activity.save(update_fields=("status", "updated_at"))
    return activity, True


def process_activity_timeouts(*, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        expired_payment_count = expire_pending_participation_orders(now=now)
    processed_activity_count = 0
    activity_ids = list(
        Activity.objects.filter(
            status=Activity.Status.RECRUITING,
            formation_deadline__lte=now,
        ).values_list("id", flat=True)
    )
    for activity_id in activity_ids:
        _, changed = fail_unformed_activity(activity_id=activity_id, now=now)
        processed_activity_count += int(changed)
    started_activity_count = 0
    formed_ids = list(
        Activity.objects.filter(
            status=Activity.Status.FORMED,
            starts_at__lte=now,
        ).values_list("id", flat=True)
    )
    for activity_id in formed_ids:
        _, changed = start_formed_activity(activity_id=activity_id, now=now)
        started_activity_count += int(changed)

    completed_activity_count = 0
    in_progress_ids = list(
        Activity.objects.filter(
            status=Activity.Status.IN_PROGRESS,
            ends_at__lte=now,
        ).values_list("id", flat=True)
    )
    for activity_id in in_progress_ids:
        _, changed = complete_started_activity(activity_id=activity_id, now=now)
        completed_activity_count += int(changed)

    settlement_created_count = 0
    settlement_error_count = 0
    completed_without_settlement_ids = list(
        Activity.objects.filter(
            status=Activity.Status.COMPLETED,
            settlement__isnull=True,
        ).values_list("id", flat=True)
    )
    for activity_id in completed_without_settlement_ids:
        try:
            _, created = ensure_activity_settlement(activity_id=activity_id, now=now)
            settlement_created_count += int(created)
        except ValidationError:
            settlement_error_count += 1

    settlement_advanced_count = 0
    settlement_ids = list(
        ActivitySettlement.objects.exclude(
            status=ActivitySettlement.Status.SETTLED
        ).values_list("id", flat=True)
    )
    for settlement_id in settlement_ids:
        try:
            _, changed = advance_activity_settlement(
                settlement_id=settlement_id, now=now
            )
            settlement_advanced_count += int(changed)
        except ValidationError:
            settlement_error_count += 1

    return {
        "expired_payment_count": expired_payment_count,
        "processed_activity_count": processed_activity_count,
        "started_activity_count": started_activity_count,
        "completed_activity_count": completed_activity_count,
        "settlement_created_count": settlement_created_count,
        "settlement_advanced_count": settlement_advanced_count,
        "settlement_error_count": settlement_error_count,
    }
