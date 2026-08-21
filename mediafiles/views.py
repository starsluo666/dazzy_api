from pathlib import PurePosixPath
from uuid import uuid4
import warnings

from django.conf import settings
from django.utils import timezone
from PIL import Image, UnidentifiedImageError
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import MediaAsset
from .services import build_home_card_assets, build_media_url, upload_public_stream


class HomeCardAssetView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return Response({"data": build_home_card_assets()})


class ActivityCoverUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    allowed_types = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    max_size = 10 * 1024 * 1024
    max_pixels = 25_000_000
    image_formats = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}

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

    def post(self, request):
        uploaded = request.FILES.get("file")
        if not uploaded:
            raise ValidationError({"file": "请选择活动封面。"})
        extension = self.allowed_types.get(uploaded.content_type)
        if not extension:
            raise ValidationError({"file": "封面仅支持 JPG、PNG 或 WebP。"})
        if uploaded.size > self.max_size:
            raise ValidationError({"file": "封面大小不能超过10MB。"})
        self.validate_image_content(uploaded)
        object_key = str(
            PurePosixPath(settings.COS_PUBLIC_PREFIX)
            / "activity-covers"
            / str(request.user.public_id)
            / f"{uuid4().hex}{extension}"
        )
        asset = MediaAsset.objects.create(
            owner=request.user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.ACTIVITY_COVER,
            object_key=object_key,
            original_filename=uploaded.name,
            content_type=uploaded.content_type,
            size_bytes=uploaded.size,
        )
        try:
            asset.etag = upload_public_stream(
                body=uploaded, object_key=object_key, content_type=uploaded.content_type
            )
        except Exception:
            asset.status = MediaAsset.Status.REJECTED
            asset.save(update_fields=("status", "updated_at"))
            raise ValidationError({"file": "封面上传失败，请稍后重试。"})
        asset.status = MediaAsset.Status.UPLOADED
        asset.uploaded_at = timezone.now()
        asset.save(update_fields=("etag", "status", "uploaded_at", "updated_at"))
        return Response(
            {"data": {"id": str(asset.pk), "url": build_media_url(asset.object_key)}},
            status=201,
        )
