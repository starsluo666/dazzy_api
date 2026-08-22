from django.conf import settings
from django.db import models
from django.db.models import Q


class ProviderFavorite(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="provider_favorites")
    provider = models.ForeignKey("providers.ProviderProfile", on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "provider_favorite"
        ordering = ("-created_at", "-id")
        constraints = [models.UniqueConstraint(fields=("user", "provider"), name="uniq_user_provider_favorite")]
        indexes = [models.Index(fields=("user", "-created_at"))]


class BrowsingHistory(models.Model):
    class TargetType(models.TextChoices):
        PROVIDER = "provider", "达人"
        ACTIVITY = "activity", "活动"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="browsing_history")
    target_type = models.CharField(max_length=16, choices=TargetType)
    provider = models.ForeignKey("providers.ProviderProfile", null=True, blank=True, on_delete=models.CASCADE, related_name="viewed_by")
    activity = models.ForeignKey("activities.Activity", null=True, blank=True, on_delete=models.CASCADE, related_name="viewed_by")
    view_count = models.PositiveIntegerField(default=1)
    viewed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "browsing_history"
        ordering = ("-viewed_at", "-id")
        constraints = [
            models.CheckConstraint(
                condition=(Q(target_type="provider", provider__isnull=False, activity__isnull=True) | Q(target_type="activity", provider__isnull=True, activity__isnull=False)),
                name="history_target_matches_type",
            ),
            models.UniqueConstraint(fields=("user", "provider"), condition=Q(provider__isnull=False), name="uniq_user_provider_history"),
            models.UniqueConstraint(fields=("user", "activity"), condition=Q(activity__isnull=False), name="uniq_user_activity_history"),
        ]
        indexes = [models.Index(fields=("user", "target_type", "-viewed_at"))]
