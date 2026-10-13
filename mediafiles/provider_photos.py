"""5:4 gallery photos. New uploads are decoded in memory; only crops enter COS."""

import base64
import math
import warnings
from contextlib import contextmanager
from io import BytesIO

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, UnidentifiedImageError
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .images import oriented_rgb_image
from .models import MediaAsset
from .services import _cos_client

MAX_BYTES = 8 * 1024 * 1024


class ProviderPhotoCropSerializer(serializers.Serializer):
    x = serializers.FloatField(min_value=0, max_value=1)
    y = serializers.FloatField(min_value=0, max_value=1)
    width = serializers.FloatField(min_value=0, max_value=1)
    height = serializers.FloatField(min_value=0, max_value=1)

    def validate(self, data):
        if not all(math.isfinite(data[key]) for key in ("x", "y", "width", "height")):
            raise ValidationError("裁剪参数无效。")
        if data["width"] <= 0 or data["height"] <= 0:
            raise ValidationError("请选择有效的照片范围。")
        if data["x"] + data["width"] > 1.000001 or data["y"] + data["height"] > 1.000001:
            raise ValidationError("裁剪范围超出照片。")
        return data


def _read_source(asset):
    response = _cos_client().get_object(Bucket=settings.COS_BUCKET, Key=asset.object_key)
    stream = response["Body"].get_raw_stream()
    try:
        return stream.read(MAX_BYTES + 1)
    finally:
        stream.close()


def source_payload(request):
    uploaded = request.FILES.get("file")
    source_id = request.data.get("source_id")
    if bool(uploaded) == bool(source_id):
        raise ValidationError("请选择一张待裁剪照片。")
    if uploaded:
        if uploaded.size > MAX_BYTES:
            raise ValidationError({"file": "照片不能超过8MB。"})
        return uploaded.read(MAX_BYTES + 1)
    source_id = serializers.UUIDField().run_validation(source_id)
    source = MediaAsset.objects.filter(
        pk=source_id,
        owner=request.user,
        scope="public",
        category="provider_photo",
        status="uploaded",
    ).first()
    if source is None:
        raise ValidationError({"source_id": "照片不存在或无权使用。"})
    try:
        return _read_source(source)
    except Exception as exc:
        raise ValidationError({"source_id": "照片暂时无法读取，请稍后重试。"}) from exc


@contextmanager
def decoded_photo(payload):
    if not payload or len(payload) > MAX_BYTES:
        raise ValidationError({"file": "照片大小无效，请重新选择。"})
    try:
        with warnings.catch_warnings(), BytesIO(payload) as buffer:
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(buffer) as source:
                if source.format not in ("JPEG", "PNG", "WEBP", "HEIF"):
                    raise ValidationError({"file": "仅支持JPG、PNG、WebP、HEIC/HEIF照片。"})
                if source.width * source.height > 25_000_000:
                    raise ValidationError({"file": "照片像素尺寸过大。"})
                with oriented_rgb_image(source) as picture:
                    yield picture
    except ValidationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        RuntimeError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValidationError({"file": "照片无法解码，请重新选择。"}) from exc


def photo_preview(payload):
    # Same orientation/decoder as the final crop, including Apple HEIF and EXIF.
    # The preview is returned inline, never persisted as a public media asset.
    with decoded_photo(payload) as picture, BytesIO() as output:
        width, height = picture.size
        picture.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        picture.save(output, format="JPEG", quality=80, exif=b"")
        return {
            "preview_base64": base64.b64encode(output.getvalue()).decode("ascii"),
            "width": width,
            "height": height,
        }


def render_provider_photo(payload, crop):
    with decoded_photo(payload) as picture:
        width, height = picture.size
        crop_width, crop_height = crop["width"] * width, crop["height"] * height
        if min(crop_width, crop_height) < 32 or abs(crop_width / crop_height - 1.25) > 0.01:
            raise ValidationError({"width": "每张照片需要保持5:4比例，请重新调整取景。"})
        box = (
            round(crop["x"] * width),
            round(crop["y"] * height),
            min(width, round((crop["x"] + crop["width"]) * width)),
            min(height, round((crop["y"] + crop["height"]) * height)),
        )
        with (
            picture.crop(box) as selected,
            selected.resize((1000, 800), Image.Resampling.LANCZOS) as result,
            BytesIO() as output,
        ):
            result.save(output, format="JPEG", quality=90, optimize=True, exif=b"")
            uploaded = SimpleUploadedFile(
                "provider-photo.jpg", output.getvalue(), content_type="image/jpeg"
            )
            uploaded.provider_photo_cropped = True
            return uploaded
