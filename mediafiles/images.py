"""Decode supported phone photos without trusting their extension or MIME type."""

from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from rest_framework.exceptions import ValidationError


# Decode the primary photo, not depth maps, auxiliary images or thumbnails.
# Leave libheif's security limits enabled; application byte/pixel limits also apply.
register_heif_opener(thumbnails=False, depth_images=False, aux_images=False, decode_threads=2)


def oriented_rgb_image(source):
    """Return an independent, display-oriented RGB image with a white alpha matte."""
    # HEIF's container rotation is already applied by libheif and its EXIF
    # orientation reset by the plugin, so this does not rotate it a second time.
    with ImageOps.exif_transpose(source) as oriented:
        if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
            with oriented.convert("RGBA") as rgba:
                image = Image.new("RGB", rgba.size, "white")
                with rgba.getchannel("A") as alpha:
                    image.paste(rgba, mask=alpha)
        else:
            image = oriented.convert("RGB")
    image.info.clear()
    return image


def normalize_heif_upload(uploaded, *, max_size):
    # content_type has been replaced by the format detected from actual bytes.
    if uploaded.content_type != "image/heif":
        return uploaded
    try:
        with Image.open(uploaded) as source:
            # HEIF can contain multiple stills. Image.open selects the primary
            # one; export that photo only, never the entire container.
            profile = source.info.get("icc_profile")
            with oriented_rgb_image(source) as image, BytesIO() as output:
                # Full decoding is required: HEIF's verify() alone is a no-op.
                # Whitelist only the colour profile; omit EXIF/GPS/XMP metadata.
                image.save(output, format="JPEG", quality=92, optimize=True, exif=b"", icc_profile=profile)
                if output.tell() > max_size:
                    raise ValidationError({"file": "图片转换后体积过大，请压缩图片后重试。"})
                return SimpleUploadedFile(uploaded.name, output.getvalue(), content_type="image/jpeg")
    except (OSError, SyntaxError, ValueError, EOFError, RuntimeError) as exc:
        raise ValidationError({"file": "图片无法解码或已损坏，请重新选择照片。"}) from exc
