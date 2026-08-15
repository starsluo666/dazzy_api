import uuid

from django.conf import settings
from django.db import models


class MediaAsset(models.Model):
    class Scope(models.TextChoices):
        PUBLIC = "public", "公开展示"
        PRIVATE = "private", "敏感资料"

    class Category(models.TextChoices):
        AVATAR = "avatar", "用户头像"
        PROVIDER_PHOTO = "provider_photo", "达人展示照片"
        CERTIFICATION = "certification", "资质材料"
        IDENTITY = "identity", "实名认证材料"
        ACTIVITY_COVER = "activity_cover", "活动封面"
        REVIEW_IMAGE = "review_image", "评价图片"
        ORDER_EVIDENCE = "order_evidence", "履约证据"
        SUPPORT_ATTACHMENT = "support_attachment", "客服附件"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        PENDING = "pending", "待上传"
        UPLOADED = "uploaded", "已上传"
        VERIFIED = "verified", "已验证"
        REJECTED = "rejected", "已拒绝"
        DELETED = "deleted", "已删除"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="media_assets"
    )
    scope = models.CharField(max_length=16, choices=Scope)
    category = models.CharField(max_length=32, choices=Category)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    object_key = models.CharField(max_length=512, unique=True)
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=127, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    etag = models.CharField(max_length=128, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "media_asset"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("owner", "category", "status"))]

    def __str__(self) -> str:
        return self.object_key
