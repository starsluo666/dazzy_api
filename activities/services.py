from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from config.geospatial import gcj02_to_wgs84

from .models import Activity, ActivityParticipation, ActivityPublishOrder
from .serializers import STANDARD_REFUND_SNAPSHOT


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


def _sync_formation_status(activity: Activity, participant_count: int) -> None:
    target = activity.status
    if participant_count >= activity.min_participants:
        target = Activity.Status.FORMED
    elif activity.status == Activity.Status.FORMED and activity.formation_deadline > timezone.now():
        target = Activity.Status.RECRUITING
    if target != activity.status:
        activity.status = target
        activity.save(update_fields=("status", "updated_at"))


@transaction.atomic
def join_activity(activity_id: int, user):
    try:
        activity = Activity.objects.select_for_update().get(pk=activity_id)
    except Activity.DoesNotExist as exc:
        raise NotFound("活动不存在。") from exc
    now = timezone.now()
    if activity.organizer_id == user.pk:
        raise PermissionDenied("组织者无需重复报名自己的活动。")
    if activity.status not in (Activity.Status.RECRUITING, Activity.Status.FORMED):
        raise ValidationError("当前活动不可报名。")
    if activity.formation_deadline <= now:
        raise ValidationError("活动报名已截止。")

    participation = ActivityParticipation.objects.filter(
        activity=activity,
        user=user,
    ).first()
    if participation and participation.status == ActivityParticipation.Status.ACTIVE:
        return participation, _active_count(activity), False

    participant_count = _active_count(activity)
    if participant_count >= activity.capacity:
        raise ValidationError("活动名额已满。")
    if participation:
        participation.status = ActivityParticipation.Status.ACTIVE
        participation.joined_at = now
        participation.cancelled_at = None
        participation.save(
            update_fields=("status", "joined_at", "cancelled_at", "updated_at")
        )
    else:
        participation = ActivityParticipation.objects.create(activity=activity, user=user)
    participant_count += 1
    _sync_formation_status(activity, participant_count)
    return participation, participant_count, True


@transaction.atomic
def cancel_activity_participation(activity_id: int, user) -> int:
    try:
        activity = Activity.objects.select_for_update().get(pk=activity_id)
    except Activity.DoesNotExist as exc:
        raise NotFound("活动不存在。") from exc
    if activity.starts_at <= timezone.now():
        raise ValidationError("活动已经开始，无法取消报名。")
    participation = ActivityParticipation.objects.filter(
        activity=activity,
        user=user,
        status=ActivityParticipation.Status.ACTIVE,
    ).first()
    if not participation:
        raise ValidationError("你尚未报名该活动。")
    participation.status = ActivityParticipation.Status.CANCELLED
    participation.cancelled_at = timezone.now()
    participation.save(update_fields=("status", "cancelled_at", "updated_at"))
    participant_count = _active_count(activity)
    _sync_formation_status(activity, participant_count)
    return participant_count
