"""Bake the identity-use notice into new private ID images before storage."""

from io import BytesIO
import os
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageDraw, ImageFont, ImageOps
from rest_framework.exceptions import ValidationError


WATERMARK_TEXT = "仅用于达人验证"


class WatermarkFontUnavailable(Exception):
    pass


def watermark_font(size):
    configured = getattr(settings, "PROVIDER_IDENTITY_WATERMARK_FONT_PATH", "")
    # The Docker runtime installs WenQuanYi. Windows uses its installed font;
    # no proprietary font is copied into the application or redistributed.
    candidates = [configured] if configured else [
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"),
    ]
    for candidate in candidates:
        try:
            font = ImageFont.truetype(candidate, size=size)
            missing = font.getmask("\U0010ffff")
            missing_signature = (missing.size, bytes(missing))
            glyphs = [font.getmask(char) for char in WATERMARK_TEXT]
            if any((glyph.size, bytes(glyph)) == missing_signature for glyph in glyphs):
                continue
            return font
        except OSError:
            continue
    # Never silently store an unprotected original or render replacement boxes.
    raise WatermarkFontUnavailable("A readable Chinese watermark font is required")


def _stamp(short_side):
    font_size = max(8, round(short_side * 0.043))
    font = watermark_font(font_size)
    left, top, right, bottom = font.getbbox(WATERMARK_TEXT)
    padding = max(2, font_size // 4)
    with Image.new("RGBA", (right - left + padding * 2, bottom - top + padding * 2)) as label:
        ImageDraw.Draw(label).text(
            (padding - left, padding - top), WATERMARK_TEXT,
            font=font, fill=(80, 100, 110, 64),
        )
        return label.rotate(25, resample=Image.Resampling.BICUBIC, expand=True)


def add_identity_watermark(uploaded, *, max_size):
    """Return a metadata-free JPEG; the uploaded original is never stored in COS.

    The caller has already verified the image format, byte and pixel limits.
    EXIF rotation is applied before re-encoding, preserving display orientation.
    """
    with Image.open(uploaded) as source:
        if getattr(source, "is_animated", False):
            raise ValidationError({"file": "身份证照片不支持动图，请上传静态图片。"})
        with ImageOps.exif_transpose(source) as oriented:
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                with oriented.convert("RGBA") as rgba:
                    image = Image.new("RGB", rgba.size, "white")
                    with rgba.getchannel("A") as alpha:
                        image.paste(rgba, mask=alpha)
            else:
                image = oriented.convert("RGB")

    with image:
        image.info.clear()  # Do not retain EXIF/GPS, comments or embedded thumbnails.
        with _stamp(min(image.size)) as stamp:
            gap = max(4, round(min(image.size) * 0.075))
            step_x, step_y = stamp.width + gap, stamp.height + gap
            with stamp.getchannel("A") as mask:
                for row, y in enumerate(range(gap // 2, image.height, step_y)):
                    start_x = gap // 2 - (step_x // 2 if row % 2 else 0)
                    for x in range(start_x, image.width, step_x):
                        image.paste(stamp, (x, y), mask)
        with BytesIO() as output:
            image.save(output, format="JPEG", quality=92, optimize=True, exif=b"")
            if output.tell() > max_size:
                raise ValidationError({"file": "图片加水印后体积过大，请压缩图片后重试。"})
            return SimpleUploadedFile(uploaded.name, output.getvalue(), content_type="image/jpeg")
