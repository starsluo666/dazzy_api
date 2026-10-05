"""Phone-format regressions using synthetic HEIF files, never real ID photos."""

from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image, ImageDraw
from pillow_heif import from_bytes
from rest_framework.exceptions import ValidationError

from accounts.models import User
from providers.models import ProviderProfile
from .images import normalize_heif_upload
from .models import MediaAsset


def heif_photo(*, orientation=None, primary_index=0, grid_tile_size=0):
    exif = Image.Exif()
    exif[315] = "synthetic-private-metadata"
    if orientation:
        exif[274] = orientation
    with Image.new("RGB", (900, 560), "white") as image, BytesIO() as output:
        options = {"exif": exif.tobytes(), "quality": 90, "tile_size": grid_tile_size}
        if primary_index:
            with Image.new("RGB", (900, 560), "#e02020") as second:
                image.save(output, format="HEIF", save_all=True, append_images=[second], primary_index=primary_index, **options)
        else:
            image.save(output, format="HEIF", **options)
        return output.getvalue()


@override_settings(PROVIDER_IDENTITY_WATERMARK_FONT_PATH="")
class HeifUploadTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.photo = heif_photo()

    def setUp(self):
        self.user = User.objects.create_user(phone="13900000982")
        ProviderProfile.objects.create(user=self.user, status=ProviderProfile.Status.APPROVED)
        self.client.force_login(self.user)
        self.stored = []
        self.private_upload = self.enterContext(patch("mediafiles.views.upload_private_stream", side_effect=self.capture))
        self.public_upload = self.enterContext(patch("mediafiles.views.upload_public_stream", side_effect=self.capture))
        self.build_url = self.enterContext(patch("mediafiles.views.build_media_url", return_value="https://media.test/photo.jpg"))

    def capture(self, *, body, object_key, content_type):
        self.assertEqual(body.tell(), 0)
        self.stored.append(body.read())
        self.assertEqual(content_type, "image/jpeg")
        self.assertTrue(object_key.endswith(".jpg"))
        return "synthetic-etag"

    def upload(self, endpoint="provider-lifestyle-photos", *, data=None, filename="IMG_TEST.HEIC", mime="image/heic", kind=None):
        fields = {"file": SimpleUploadedFile(filename, data if data is not None else self.photo, content_type=mime)}
        if kind:
            fields["kind"] = kind
        return self.client.post(f"/api/v1/media/{endpoint}/", fields)

    def assert_jpeg(self, response, *, scope, size=(900, 560)):
        self.assertEqual(response.status_code, 201, response.data)
        asset = MediaAsset.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(asset.status, MediaAsset.Status.UPLOADED)
        self.assertEqual(asset.content_type, "image/jpeg")
        self.assertEqual(asset.scope, scope)
        self.assertEqual(asset.size_bytes, len(self.stored[-1]))
        self.assertNotIn(b"synthetic-private-metadata", self.stored[-1])
        with Image.open(BytesIO(self.stored[-1])) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, size)
            self.assertEqual(dict(image.getexif()), {})
            self.assertFalse(getattr(image, "is_animated", False))
        return asset

    def test_lifestyle_heic_and_heif_ignore_incorrect_phone_mime_and_filename(self):
        for filename, mime in (
            ("IMG_TEST.HEIC", "image/heic"), ("phone.heif", "image/heif"),
            ("wx_temp.jpg", "image/jpeg"), ("wx_temp", "application/octet-stream"),
        ):
            with self.subTest(filename=filename, mime=mime):
                asset = self.assert_jpeg(self.upload(filename=filename, mime=mime), scope=MediaAsset.Scope.PUBLIC)
                self.assertEqual(asset.original_filename, filename)
        self.private_upload.assert_not_called()

    def test_identity_front_back_and_legacy_heif_are_watermarked(self):
        for kind in ("identity_front_photo", "identity_back_photo", None):
            with self.subTest(kind=kind):
                asset = self.assert_jpeg(self.upload("provider-identities", kind=kind), scope=MediaAsset.Scope.PRIVATE)
                self.build_url.assert_called_with(asset.object_key, private=True)
                with Image.open(BytesIO(self.stored[-1])) as image:
                    self.assertLess(image.convert("L").getextrema()[0], 235)
        self.assertEqual(self.private_upload.call_count, 3)
        self.public_upload.assert_not_called()

    def test_face_heif_is_displayable_jpeg_without_watermark(self):
        response = self.upload("provider-identities", kind="identity_face_photo")
        self.assert_jpeg(response, scope=MediaAsset.Scope.PRIVATE)
        with Image.open(BytesIO(self.stored[-1])) as image:
            self.assertGreater(image.convert("L").getextrema()[0], 250)

    def test_phone_grid_tiles_are_supported(self):
        data = heif_photo(grid_tile_size=512)
        with Image.open(BytesIO(data)) as source:
            self.assertEqual(source.info["tiling"]["tile_width"], 512)
        response = self.upload(data=data)
        self.assert_jpeg(response, scope=MediaAsset.Scope.PUBLIC)

    def test_ten_bit_heif_can_be_decoded_to_standard_jpeg(self):
        size = (900, 560)
        high_depth = from_bytes("RGB;16", size, b"\xff\xff" * (size[0] * size[1] * 3))
        with BytesIO() as output:
            high_depth.save(output, quality=90)
            data = output.getvalue()
        with Image.open(BytesIO(data)) as source:
            self.assertEqual(source.info["bit_depth"], 10)
        response = self.upload(data=data)
        self.assert_jpeg(response, scope=MediaAsset.Scope.PUBLIC)

    def test_orientation_is_applied_once_before_both_normalization_and_watermark(self):
        # Red on the left should end up above blue after a 90-degree CW rotation.
        with Image.new("RGB", (900, 560), "blue") as source, BytesIO() as output:
            ImageDraw.Draw(source).rectangle((0, 0, 449, 559), fill="red")
            exif = Image.Exif()
            exif[274] = 6
            source.save(output, format="HEIF", exif=exif.tobytes(), quality=90)
            rotated = output.getvalue()
        for endpoint in ("provider-lifestyle-photos", "provider-identities"):
            with self.subTest(endpoint=endpoint):
                response = self.upload(endpoint, data=rotated)
                scope = MediaAsset.Scope.PRIVATE if endpoint == "provider-identities" else MediaAsset.Scope.PUBLIC
                self.assert_jpeg(response, scope=scope, size=(560, 900))
                with Image.open(BytesIO(self.stored[-1])) as image:
                    red, _, blue = image.getpixel((280, 200))
                    self.assertGreater(red, blue + 100)
                    red, _, blue = image.getpixel((280, 700))
                    self.assertGreater(blue, red + 100)

    def test_multi_image_container_uses_primary_photo_and_does_not_look_like_animation(self):
        data = heif_photo(primary_index=1)
        for endpoint in ("provider-lifestyle-photos", "provider-identities"):
            with self.subTest(endpoint=endpoint):
                response = self.upload(endpoint, data=data)
                scope = MediaAsset.Scope.PRIVATE if endpoint == "provider-identities" else MediaAsset.Scope.PUBLIC
                self.assert_jpeg(response, scope=scope)
                with Image.open(BytesIO(self.stored[-1])) as image:
                    red, green, blue = image.getpixel((450, 280))
                    self.assertGreater(red, green + 100)
                    self.assertGreater(red, blue + 100)

    def test_shared_order_evidence_path_also_remains_private(self):
        response = self.upload("order-evidence")
        asset = self.assert_jpeg(response, scope=MediaAsset.Scope.PRIVATE)
        self.assertEqual(asset.category, MediaAsset.Category.ORDER_EVIDENCE)
        self.public_upload.assert_not_called()

    def test_every_customer_photo_endpoint_accepts_apple_photo(self):
        for endpoint, scope in (
            ("avatars", MediaAsset.Scope.PUBLIC),
            ("activity-covers", MediaAsset.Scope.PUBLIC),
            ("provider-lifestyle-photos", MediaAsset.Scope.PUBLIC),
            ("review-images", MediaAsset.Scope.PUBLIC),
            ("support-attachments", MediaAsset.Scope.PRIVATE),
            ("order-evidence", MediaAsset.Scope.PRIVATE),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.upload(endpoint, filename="wx_temp", mime="application/octet-stream")
                asset = self.assert_jpeg(response, scope=scope)
                if scope == MediaAsset.Scope.PRIVATE:
                    self.build_url.assert_called_with(asset.object_key, private=True)
                if endpoint == "avatars":
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.avatar_object_key, asset.object_key)

    def test_invalid_heif_and_fake_heif_file_are_rejected_without_storage(self):
        for data in (b"not an image", b"\x00\x00\x00\x18ftypheic", self.photo[:-80]):
            with self.subTest(size=len(data)):
                response = self.upload(data=data)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.stored, [])
        self.assertFalse(MediaAsset.objects.exists())

    @patch("pillow_heif.as_plugin.HeifImageFile.load", side_effect=EOFError("synthetic decode failure"))
    def test_header_only_validation_cannot_bypass_full_decode(self, _load):
        for endpoint in ("provider-lifestyle-photos", "provider-identities"):
            with self.subTest(endpoint=endpoint):
                response = self.upload(endpoint)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNotIn("synthetic decode failure", str(response.data))
        self.assertEqual(self.stored, [])
        self.assertFalse(MediaAsset.objects.exists())

    def test_image_limits_still_apply_before_decode(self):
        with patch("mediafiles.images.oriented_rgb_image") as decode:
            with patch("mediafiles.views.PublicImageUploadView.max_pixels", 100):
                self.assertEqual(self.upload().status_code, 400)
            with patch("mediafiles.views.ProviderLifestylePhotoUploadView.max_size", 100):
                self.assertEqual(self.upload().status_code, 400)
            decode.assert_not_called()
        self.assertEqual(self.stored, [])
        self.assertFalse(MediaAsset.objects.exists())

    def test_encoded_size_is_also_limited(self):
        uploaded = SimpleUploadedFile("photo.heic", self.photo, content_type="image/heif")
        with self.assertRaisesMessage(ValidationError, "图片转换后体积过大"):
            normalize_heif_upload(uploaded, max_size=1)
