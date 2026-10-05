from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image, ImageFont
from rest_framework.exceptions import ValidationError

from accounts.models import User
from providers.models import ProviderProfile
from .identity_watermark import WATERMARK_TEXT, WatermarkFontUnavailable, add_identity_watermark
from .models import MediaAsset


def synthetic_image(image_format="PNG", *, mode="RGB", size=(900, 560), exif=None):
    # Synthetic images only: no real identity or bank data in tests.
    with Image.new("RGB", size, "white") as base, base.convert(mode) as image, BytesIO() as output:
        image.save(output, format=image_format, **({"exif": exif} if exif else {}))
        return output.getvalue()


@override_settings(PROVIDER_IDENTITY_WATERMARK_FONT_PATH="")
class IdentityWatermarkImageTests(SimpleTestCase):
    def test_supported_formats_get_readable_light_watermark_and_clean_jpeg(self):
        self.assertEqual(WATERMARK_TEXT, "仅用于达人验证")
        for image_format, mode in (("JPEG", "RGB"), ("PNG", "RGBA"), ("PNG", "P"), ("WEBP", "RGB"), ("JPEG", "CMYK")):
            with self.subTest(image_format=image_format, mode=mode):
                uploaded = SimpleUploadedFile("document.png", synthetic_image(image_format, mode=mode))
                result = add_identity_watermark(uploaded, max_size=8 * 1024 * 1024)
                self.assertEqual(result.content_type, "image/jpeg")
                self.assertEqual(result.name, "document.png")
                self.assertEqual(result.size, len(result.read()))
                result.seek(0)
                with Image.open(result) as image:
                    self.assertEqual(image.size, (900, 560))
                    self.assertEqual(image.format, "JPEG")
                    self.assertEqual(image.mode, "RGB")
                    minimum, maximum = image.convert("L").getextrema()
                    self.assertGreater(minimum, 190)  # No opaque text/bar obscuring content.
                    self.assertLess(minimum, 235)  # Notice is actually burned into pixels.
                    self.assertEqual(maximum, 255)

    def test_phone_exif_orientation_is_applied_and_metadata_removed(self):
        exif = Image.Exif()
        exif[274] = 6  # Rotate 90 degrees clockwise.
        exif[315] = "synthetic private metadata"
        uploaded = SimpleUploadedFile("phone.jpg", synthetic_image("JPEG", exif=exif))
        result = add_identity_watermark(uploaded, max_size=8 * 1024 * 1024)
        self.assertNotIn(b"synthetic private metadata", result.read())
        result.seek(0)
        with Image.open(result) as image:
            self.assertEqual(image.size, (560, 900))
            self.assertEqual(dict(image.getexif()), {})

    def test_transparent_background_becomes_white_not_black(self):
        with Image.new("RGBA", (900, 560), (0, 0, 0, 0)) as image, BytesIO() as output:
            image.save(output, format="PNG")
            uploaded = SimpleUploadedFile("alpha.png", output.getvalue())
        result = add_identity_watermark(uploaded, max_size=8 * 1024 * 1024)
        with Image.open(result) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_processed_size_limit_is_checked(self):
        with self.assertRaisesMessage(ValidationError, "图片加水印后体积过大"):
            add_identity_watermark(SimpleUploadedFile("id.png", synthetic_image()), max_size=1)

    @override_settings(PROVIDER_IDENTITY_WATERMARK_FONT_PATH="configured-latin-only-font.ttf")
    def test_font_without_chinese_glyphs_cannot_silently_render_boxes(self):
        latin_font = ImageFont.load_default()
        with patch("mediafiles.identity_watermark.ImageFont.truetype", return_value=latin_font):
            with self.assertRaises(WatermarkFontUnavailable):
                add_identity_watermark(SimpleUploadedFile("id.png", synthetic_image()), max_size=8 * 1024 * 1024)

    def test_animated_images_are_rejected_instead_of_silently_losing_frames(self):
        with Image.new("RGB", (900, 560), "white") as first, Image.new("RGB", (900, 560), "blue") as second, BytesIO() as output:
            first.save(output, format="WEBP", save_all=True, append_images=[second], duration=100)
            uploaded = SimpleUploadedFile("animated.webp", output.getvalue())
        with self.assertRaisesMessage(ValidationError, "身份证照片不支持动图"):
            add_identity_watermark(uploaded, max_size=8 * 1024 * 1024)


@override_settings(PROVIDER_IDENTITY_WATERMARK_FONT_PATH="")
class IdentityWatermarkUploadTests(TestCase):
    url = "/api/v1/media/provider-identities/"

    def setUp(self):
        self.user = User.objects.create_user(phone="13900000981")
        self.provider = ProviderProfile.objects.create(user=self.user, status=ProviderProfile.Status.APPROVED)
        self.client.force_login(self.user)
        self.private_bytes = []
        self.private_upload = self.enterContext(patch("mediafiles.views.upload_private_stream", side_effect=self.capture))
        self.public_upload = self.enterContext(patch("mediafiles.views.upload_public_stream", return_value="public-etag"))
        self.build_url = self.enterContext(patch("mediafiles.views.build_media_url", return_value="https://private.test/signed-photo"))

    def capture(self, *, body, **kwargs):
        self.private_bytes.append(body.read())
        return "watermarked-etag"

    def upload(self, kind=None, **kwargs):
        data = {"file": SimpleUploadedFile("mobile.png", synthetic_image(), content_type="image/jpeg")}
        if kind is not None:
            data["kind"] = kind
        data.update(kwargs)
        return self.client.post(self.url, data)

    def test_front_back_and_legacy_uploads_store_only_watermarked_private_bytes(self):
        for kind in ("identity_front_photo", "identity_back_photo", None):
            with self.subTest(kind=kind):
                response = self.upload(kind)
                self.assertEqual(response.status_code, 201, response.data)
                asset = MediaAsset.objects.get(pk=response.data["data"]["id"])
                self.assertEqual(asset.scope, MediaAsset.Scope.PRIVATE)
                self.assertEqual(asset.category, MediaAsset.Category.IDENTITY)
                self.assertEqual(asset.status, MediaAsset.Status.UPLOADED)
                self.assertEqual(asset.content_type, "image/jpeg")
                self.assertTrue(asset.object_key.endswith(".jpg"))
                self.assertEqual(asset.original_filename, "mobile.png")
                self.assertEqual(asset.size_bytes, len(self.private_bytes[-1]))
                self.build_url.assert_called_with(asset.object_key, private=True)
                with Image.open(BytesIO(self.private_bytes[-1])) as stored:
                    self.assertEqual(stored.format, "JPEG")
                    self.assertLess(stored.convert("L").getextrema()[0], 235)
        self.assertEqual(self.private_upload.call_count, 3)
        self.public_upload.assert_not_called()

    def test_face_photo_keeps_original_bytes(self):
        response = self.upload("identity_face_photo")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.private_bytes[0], synthetic_image())
        asset = MediaAsset.objects.get()
        self.assertEqual(asset.content_type, "image/png")
        self.assertTrue(asset.object_key.endswith(".png"))

    @override_settings(PROVIDER_IDENTITY_WATERMARK_FONT_PATH="/missing-font/identity.ttf")
    def test_missing_font_fails_closed_before_asset_or_storage_write(self):
        with self.assertLogs("mediafiles.views", level="ERROR"):
            response = self.upload("identity_front_photo")
        self.assertEqual(response.status_code, 503)
        self.assertIn("水印服务暂不可用", str(response.data))
        self.private_upload.assert_not_called()
        self.assertFalse(MediaAsset.objects.exists())

    def test_invalid_kind_or_corrupt_content_never_reaches_storage(self):
        for kind in ("", "anything", "null"):
            self.assertEqual(self.upload(kind).status_code, 400)
        response = self.upload(file=SimpleUploadedFile("bad.jpg", b"not an image", content_type="image/jpeg"))
        self.assertEqual(response.status_code, 400)
        self.private_upload.assert_not_called()
        self.assertFalse(MediaAsset.objects.exists())

    @patch("mediafiles.views.add_identity_watermark", side_effect=OSError("synthetic decode error"))
    def test_decode_failure_shows_safe_error_not_a_successful_original_upload(self, _watermark):
        response = self.upload()
        self.assertEqual(response.status_code, 400)
        self.assertIn("图片处理失败", str(response.data))
        self.assertNotIn("synthetic decode error", str(response.data))
        self.private_upload.assert_not_called()
        self.assertFalse(MediaAsset.objects.exists())

    def test_original_size_and_pixel_limits_still_apply(self):
        with patch("mediafiles.views.ProviderIdentityPhotoUploadView.max_size", 1):
            self.assertEqual(self.upload().status_code, 400)
        with patch("mediafiles.views.ProviderIdentityPhotoUploadView.max_pixels", 1):
            self.assertEqual(self.upload().status_code, 400)
        self.private_upload.assert_not_called()
        self.assertFalse(MediaAsset.objects.exists())

    def test_approval_and_login_are_still_required(self):
        self.provider.status = ProviderProfile.Status.PENDING
        self.provider.save(update_fields=["status"])
        self.assertEqual(self.upload().status_code, 400)
        self.client.logout()
        self.assertEqual(self.upload().status_code, 401)
        self.private_upload.assert_not_called()

    @patch("mediafiles.views.add_identity_watermark")
    def test_lifestyle_upload_is_not_watermarked(self, watermark):
        response = self.client.post("/api/v1/media/provider-lifestyle-photos/", {
            "file": SimpleUploadedFile("life.png", synthetic_image(), content_type="image/png"),
        })
        self.assertEqual(response.status_code, 201)
        watermark.assert_not_called()
        self.private_upload.assert_not_called()
        self.public_upload.assert_called_once()
