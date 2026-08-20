from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from .models import Activity, ActivityParticipation


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
