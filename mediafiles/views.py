from pathlib import PurePosixPath
from uuid import uuid4
import logging
import warnings

from django.conf import settings
from django.utils import timezone
from PIL import Image, UnidentifiedImageError
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import MediaAsset
from .services import (
    build_home_card_assets,
    build_media_url,
    delete_public_object,
    upload_private_stream,
    upload_public_stream,
)


logger = logging.getLogger(__name__)


class HomeCardAssetView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"data": build_home_card_assets()})


class PublicImageUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    allowed_types = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    max_pixels = 25_000_000
    image_formats = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
    max_size = 10 * 1024 * 1024
    folder = "images"
    category = MediaAsset.Category.OTHER
    scope = MediaAsset.Scope.PUBLIC
    prefix_setting = "COS_PUBLIC_PREFIX"
    field_label = "图片"

    def upload_stream(self, *, body, object_key: str, content_type: str) -> str:
        return upload_public_stream(body=body, object_key=object_key, content_type=content_type)

    def validate_image_content(self, uploaded) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image = Image.open(uploaded)
                width, height = image.size
                if image.format != self.image_formats[uploaded.content_type]:
                    raise ValidationError({"file": "图片内容与文件类型不一致。"})
                if width <= 0 or height <= 0 or width * height > self.max_pixels:
                    raise ValidationError({"file": "图片像素尺寸过大。"})
                image.verify()
        except ValidationError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValidationError({"file": "图片文件已损坏或格式无效。"}) from exc
        finally:
            uploaded.seek(0)

    def create_asset(self, request):
        uploaded = request.FILES.get("file")
        if not uploaded:
            raise ValidationError({"file": f"请选择{self.field_label}。"})
        extension = self.allowed_types.get(uploaded.content_type)
        if not extension:
            raise ValidationError({"file": f"{self.field_label}仅支持 JPG、PNG 或 WebP。"})
        if uploaded.size > self.max_size:
            size_mb = self.max_size // (1024 * 1024)
            raise ValidationError({"file": f"{self.field_label}大小不能超过{size_mb}MB。"})
        self.validate_image_content(uploaded)
        object_key = str(
            PurePosixPath(getattr(settings, self.prefix_setting))
            / self.folder
            / str(request.user.public_id)
            / f"{uuid4().hex}{extension}"
        )
        asset = MediaAsset.objects.create(
            owner=request.user,
            scope=self.scope,
            category=self.category,
            object_key=object_key,
            original_filename=uploaded.name,
            content_type=uploaded.content_type,
            size_bytes=uploaded.size,
        )
        try:
            asset.etag = self.upload_stream(
                body=uploaded, object_key=object_key, content_type=uploaded.content_type
            )
        except Exception:
            logger.exception(
                "Public media upload failed",
                extra={"media_category": self.category, "object_key": object_key},
            )
            asset.status = MediaAsset.Status.REJECTED
            asset.save(update_fields=("status", "updated_at"))
            raise ValidationError({"file": f"{self.field_label}上传失败，请稍后重试。"})
        asset.status = MediaAsset.Status.UPLOADED
        asset.uploaded_at = timezone.now()
        asset.save(update_fields=("etag", "status", "uploaded_at", "updated_at"))
        return asset


class ActivityCoverUploadView(PublicImageUploadView):
    folder = "activity-covers"
    category = MediaAsset.Category.ACTIVITY_COVER
    field_label = "活动封面"

    def post(self, request):
        asset = self.create_asset(request)
        return Response(
            {"data": {"id": str(asset.pk), "url": build_media_url(asset.object_key)}},
            status=201,
        )


class ProviderLifestylePhotoUploadView(PublicImageUploadView):
    max_size = 8 * 1024 * 1024
    folder = "provider-photos"
    category = MediaAsset.Category.PROVIDER_PHOTO
    field_label = "生活照"

    def post(self, request):
        asset = self.create_asset(request)
        return Response(
            {"data": {"id": str(asset.pk), "url": build_media_url(asset.object_key)}},
            status=201,
        )


class OrderEvidenceUploadView(PublicImageUploadView):
    max_size = 8 * 1024 * 1024
    folder = "order-evidence"
    category = MediaAsset.Category.ORDER_EVIDENCE
    scope = MediaAsset.Scope.PRIVATE
    prefix_setting = "COS_PRIVATE_PREFIX"
    field_label = "履约照片"

    def upload_stream(self, *, body, object_key: str, content_type: str) -> str:
        return upload_private_stream(body=body, object_key=object_key, content_type=content_type)

    def post(self, request):
        asset = self.create_asset(request)
        return Response(
            {
                "data": {
                    "id": str(asset.pk),
                    "url": build_media_url(asset.object_key, private=True),
                }
            },
            status=201,
        )


class ReviewImageUploadView(PublicImageUploadView):
    max_size = 5 * 1024 * 1024
    folder = "review-images"
    category = MediaAsset.Category.REVIEW_IMAGE
    field_label = "评价图片"

    def post(self, request):
        asset = self.create_asset(request)
        return Response(
            {"data": {"id": str(asset.pk), "url": build_media_url(asset.object_key)}},
            status=201,
        )


class SupportAttachmentUploadView(PublicImageUploadView):
    max_size = 5 * 1024 * 1024
    folder = "support-attachments"
    category = MediaAsset.Category.SUPPORT_ATTACHMENT
    scope = MediaAsset.Scope.PRIVATE
    prefix_setting = "COS_PRIVATE_PREFIX"
    field_label = "证据图片"

    def upload_stream(self, *, body, object_key: str, content_type: str) -> str:
        return upload_private_stream(body=body, object_key=object_key, content_type=content_type)

    def post(self, request):
        asset = self.create_asset(request)
        return Response(
            {
                "data": {
                    "id": str(asset.pk),
                    "url": build_media_url(asset.object_key, private=True),
                }
            },
            status=201,
        )


class AvatarUploadView(PublicImageUploadView):
    max_size = 5 * 1024 * 1024
    folder = "avatars"
    category = MediaAsset.Category.AVATAR
    field_label = "头像"

    def post(self, request):
        old_asset = None
        if request.user.avatar_object_key:
            old_asset = (
                MediaAsset.objects.filter(
                    owner=request.user,
                    category=MediaAsset.Category.AVATAR,
                    object_key=request.user.avatar_object_key,
                )
                .exclude(status=MediaAsset.Status.DELETED)
                .first()
            )
        asset = self.create_asset(request)
        request.user.avatar_object_key = asset.object_key
        request.user.save(update_fields=("avatar_object_key",))
        if old_asset:
            try:
                delete_public_object(object_key=old_asset.object_key)
            except Exception:
                logger.exception("Failed to delete replaced avatar asset %s", old_asset.pk)
            else:
                old_asset.status = MediaAsset.Status.DELETED
                old_asset.save(update_fields=("status", "updated_at"))
        return Response(
            {"data": {"id": str(asset.pk), "url": build_media_url(asset.object_key)}},
            status=201,
        )
