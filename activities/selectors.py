from django.utils import timezone

from .models import Activity


def upcoming_public_activities():
    return Activity.objects.filter(
        status__in=(Activity.Status.RECRUITING, Activity.Status.FORMED),
        starts_at__gt=timezone.now(),
    ).select_related("category", "organizer", "cover")
