from io import BytesIO
import base64
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User
from .models import MediaAsset


class ProviderPhotoTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(phone="13966110001")
        self.other = User.objects.create_user(phone="13966110002")
        self.source = MediaAsset.objects.create(
            owner=self.user,
            scope="public",
            status="uploaded",
            category="provider_photo",
            object_key="test/original.jpg",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.payload = {
            "source_id": str(self.source.pk),
            "x": 0,
            "y": 0.1,
            "width": 1,
            "height": 0.4,
        }
        self.uploaded = []
        self.read = self.enterContext(
            patch("mediafiles.provider_photos._read_source", return_value=self.photo())
        )
        self.upload = self.enterContext(
            patch("mediafiles.views.upload_public_stream", side_effect=self.capture)
        )
        self.enterContext(
            patch("mediafiles.views.build_media_url", return_value="https://media.test/crop.jpg")
        )

    @staticmethod
    def photo(rotated=False):
        # Display-oriented 500x1000: green upper half and blue lower half.
        image = Image.new("RGB", (500, 1000), "blue")
        image.paste("green", (0, 0, 500, 500))
        exif = Image.Exif()
        if rotated:
            image = image.transpose(Image.Transpose.ROTATE_90)
            exif[274] = 6
        exif[270] = "private metadata"
        output = BytesIO()
        image.save(output, format="JPEG", exif=exif)
        return output.getvalue()

    def capture(self, *, body, object_key, content_type):
        self.uploaded.append((body.read(), object_key, content_type))
        return "crop-etag"

    def crop(self, **changes):
        return self.client.post(
            "/api/v1/media/provider-photos/crop/", {**self.payload, **changes}, format="json"
        )

    def test_crop_creates_separate_public_asset_with_no_metadata(self):
        response = self.crop()
        self.assertEqual(response.status_code, 201, response.data)
        crop = MediaAsset.objects.get(pk=response.data["data"]["id"])
        self.assertNotEqual(crop.pk, self.source.pk)
        self.assertEqual(
            (crop.owner_id, crop.category, crop.scope, crop.status),
            (self.user.pk, "provider_photo", "public", "uploaded"),
        )
        self.source.refresh_from_db()
        self.assertEqual(self.source.object_key, "test/original.jpg")
        payload, key, content_type = self.uploaded[0]
        self.assertIn("provider-photos-cropped/", key)
        self.assertTrue(crop.provider_photo_cropped)
        self.assertEqual(content_type, "image/jpeg")
        with Image.open(BytesIO(payload)) as image:
            self.assertEqual(image.size, (1000, 800))
            self.assertFalse(image.getexif())
            r, g, b = image.getpixel((500, 400))
            self.assertGreater(g, r + b)

    def test_exif_orientation_matches_display_coordinates(self):
        self.read.return_value = self.photo(rotated=True)
        self.assertEqual(self.crop().status_code, 201)
        with Image.open(BytesIO(self.uploaded[0][0])) as image:
            r, g, b = image.getpixel((500, 400))
            self.assertGreater(g, r + b)

    def test_ownership_scope_category_and_state_checked_before_read(self):
        for fields in (
            {"owner": self.other},
            {"scope": "private"},
            {"category": "identity"},
            {"status": "pending"},
            {"status": "rejected"},
        ):
            asset = MediaAsset.objects.create(
                owner=fields.get("owner", self.user),
                scope=fields.get("scope", "public"),
                category=fields.get("category", "provider_photo"),
                status=fields.get("status", "uploaded"),
                object_key=f"test/{uuid4()}.jpg",
            )
            self.assertEqual(self.crop(source_id=str(asset.pk)).status_code, 400)
        self.assertEqual(self.crop(source_id=str(uuid4())).status_code, 400)
        self.read.assert_not_called()
        self.upload.assert_not_called()

    def test_invalid_bounds_ratio_and_non_finite_values(self):
        for fields in (
            {"x": -0.1},
            {"x": 0.1},
            {"width": 0},
            {"height": 1},
            {"x": "NaN"},
            {"width": "Infinity"},
            {"width": 0.01, "height": 0.004},
        ):
            self.assertEqual(self.crop(**fields).status_code, 400, fields)
        self.upload.assert_not_called()

    def test_failures_do_not_change_original(self):
        self.read.return_value = b"not an image"
        self.assertEqual(self.crop().status_code, 400)
        self.read.side_effect = OSError("offline")
        self.assertEqual(self.crop().status_code, 400)
        self.read.side_effect = None
        self.read.return_value = self.photo()
        self.upload.side_effect = OSError("upload failed")
        self.assertEqual(self.crop().status_code, 400)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "uploaded")
        self.assertEqual(MediaAsset.objects.filter(status="uploaded").count(), 1)

    def test_authentication_and_rate_limit(self):
        self.client.force_authenticate(None)
        self.assertIn(self.crop().status_code, (401, 403))
        self.client.force_authenticate(self.user)
        for _ in range(30):
            self.assertEqual(self.crop(source_id=str(uuid4())).status_code, 400)
        self.assertEqual(self.crop().status_code, 429)
        self.read.assert_not_called()

    def test_new_upload_preview_is_not_persisted_and_only_crop_is_stored(self):
        before = MediaAsset.objects.count()
        response = self.client.post(
            "/api/v1/media/provider-photos/preview/",
            {
                "file": SimpleUploadedFile(
                    "phone.heic", self.photo(rotated=True), content_type="application/octet-stream"
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(MediaAsset.objects.count(), before)
        self.upload.assert_not_called()
        with Image.open(
            BytesIO(base64.b64decode(response.data["data"]["preview_base64"]))
        ) as preview:
            self.assertEqual(preview.size, (500, 1000))
            self.assertFalse(preview.getexif())
        payload = {key: value for key, value in self.payload.items() if key != "source_id"}
        payload["file"] = SimpleUploadedFile(
            "phone.heic", self.photo(rotated=True), content_type="application/octet-stream"
        )
        response = self.client.post(
            "/api/v1/media/provider-photos/crop/", payload, format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(MediaAsset.objects.count(), before + 1)
        self.read.assert_not_called()
        self.assertTrue(response.data["data"]["cropped"])

    def test_preview_ownership_and_missing_ambiguous_source(self):
        for payload in ({}, {"source_id": str(uuid4())}, {"source_id": "bad-uuid"}):
            response = self.client.post(
                "/api/v1/media/provider-photos/preview/", payload, format="json"
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/v1/media/provider-photos/preview/",
                {
                    "source_id": str(self.source.pk),
                    "file": SimpleUploadedFile("photo.jpg", self.photo()),
                },
                format="multipart",
            ).status_code,
            400,
        )
        self.read.assert_not_called()
        self.upload.assert_not_called()

    def test_apple_heif_preview_and_crop(self):
        output = BytesIO()
        with Image.new("RGB", (500, 1000), "green") as image:
            image.save(output, format="HEIF")
        self.read.return_value = output.getvalue()
        preview = self.client.post(
            "/api/v1/media/provider-photos/preview/",
            {"source_id": str(self.source.pk)},
            format="json",
        )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(self.crop().status_code, 201)
        with Image.open(BytesIO(self.uploaded[0][0])) as cropped:
            self.assertEqual(cropped.size, (1000, 800))

    def test_preview_rejects_invalid_type_and_oversize(self):
        for content in (b"invalid", b"x" * (8 * 1024 * 1024 + 1)):
            response = self.client.post(
                "/api/v1/media/provider-photos/preview/",
                {"file": SimpleUploadedFile("photo.jpg", content)},
                format="multipart",
            )
            self.assertEqual(response.status_code, 400)
        self.upload.assert_not_called()
