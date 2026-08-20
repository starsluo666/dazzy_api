from django.utils import timezone
from django.db.models import Count, Q

from .models import Activity, ActivityParticipation


def with_participant_count(queryset):
    return queryset.annotate(
        participant_count=Count(
            "participations",
            filter=Q(participations__status=ActivityParticipation.Status.ACTIVE),
        )
    )


def upcoming_public_activities():
    return with_participant_count(
        Activity.objects.filter(
            status__in=(Activity.Status.RECRUITING, Activity.Status.FORMED),
            starts_at__gt=timezone.now(),
        ).select_related("category", "organizer", "cover")
    )
