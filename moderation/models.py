import uuid

from django.conf import settings
from django.db import models


class ContentReview(models.Model):
    class Kind(models.TextChoices):
        NICKNAME = "nickname", "用户昵称"
        AVATAR = "avatar", "用户头像"
        PROVIDER = "provider", "达人公开资料"
        PROFILE_REVISION = "profile_revision", "达人资料修订"
        SERVICE = "service", "达人服务说明"
        SERVICE_REVISION = "service_revision", "达人服务修订"
        ACTIVITY = "activity", "活动内容"
        REVIEW = "review", "订单评价"

    class Status(models.TextChoices):
        QUEUED = "queued", "等待检测"
        PROCESSING = "processing", "检测中"
        MANUAL = "manual", "待人工复核"
        APPROVED = "approved", "内容审核通过"
        REJECTED = "rejected", "内容审核未通过"
        WITHDRAWN = "withdrawn", "已下架"
        SUPERSEDED = "superseded", "已被新内容替代"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=24, choices=Kind)
    target_id = models.PositiveBigIntegerField()
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    city_code = models.CharField(max_length=20, blank=True)
    fingerprint = models.CharField(max_length=64)
    snapshot = models.JSONField(default=dict)
    search_text = models.TextField(blank=True)
    base_value = models.CharField(max_length=512, blank=True)
    is_current = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.QUEUED)
    machine_suggestion = models.CharField(max_length=16, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    revision = models.PositiveIntegerField(default=1)
    lease_token = models.UUIDField(null=True, editable=False)
    lease_until = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(null=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="content_decisions",
        null=True,
    )
    reviewed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("kind", "target_id"),
                condition=models.Q(is_current=True),
                name="one_current_content_review",
            )
        ]
        indexes = [
            models.Index(fields=("status", "next_attempt_at")),
            models.Index(fields=("kind", "target_id")),
        ]


class ContentReviewItem(models.Model):
    review = models.ForeignKey(ContentReview, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveSmallIntegerField()
    kind = models.CharField(
        max_length=8, choices=(("text", "文字"), ("image", "图片"), ("video", "视频"))
    )
    text = models.TextField(blank=True)
    asset = models.ForeignKey("mediafiles.MediaAsset", on_delete=models.PROTECT, null=True)
    state = models.CharField(max_length=16, default="pending")
    task_id = models.CharField(max_length=128, blank=True)
    request_id = models.CharField(max_length=128, blank=True)
    suggestion = models.CharField(max_length=16, blank=True)
    label = models.CharField(max_length=64, blank=True)
    score = models.PositiveSmallIntegerField(null=True)
    error_code = models.CharField(max_length=32, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(fields=("review", "position"), name="unique_content_item")
        ]


class ContentReviewEvent(models.Model):
    review = models.ForeignKey(ContentReview, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=32)
    revision = models.PositiveIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
