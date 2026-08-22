from unittest.mock import patch
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from accounts.models import User

from .models import MediaAsset


class MediaAssetTests(TestCase):
    @staticmethod
    def valid_webp() -> bytes:
        output = BytesIO()
        Image.new("RGB", (1280, 720), "#18c7c6").save(output, format="WEBP")
        return output.getvalue()

    def test_private_identity_asset(self):
        user = User.objects.create_user(phone="13900000000")
        asset = MediaAsset.objects.create(
            owner=user,
            scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.IDENTITY,
            object_key=f"dazzy-test/private/identities/{user.public_id}/front.jpg",
        )
        self.assertEqual(asset.status, MediaAsset.Status.PENDING)
        self.assertEqual(asset.scope, MediaAsset.Scope.PRIVATE)

    @patch("mediafiles.services.build_media_url")
    def test_home_card_assets_returns_both_public_urls(self, build_media_url):
        build_media_url.side_effect = lambda key: f"https://media.test/{key}"

        response = self.client.get("/api/v1/content/home-cards/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn("provider-companion.webp", data["provider_companion_url"])
        self.assertIn("group-activity.webp", data["group_activity_url"])
        self.assertEqual(build_media_url.call_count, 2)

    @patch("mediafiles.views.build_media_url", return_value="https://media.test/cover.webp")
    @patch("mediafiles.views.upload_public_stream", return_value="cover-etag")
    def test_authenticated_user_can_upload_activity_cover(self, upload_stream, _build_url):
        user = User.objects.create_user(phone="13900000001")
        self.client.force_login(user)
        image = SimpleUploadedFile("cover.webp", self.valid_webp(), content_type="image/webp")

        response = self.client.post("/api/v1/media/activity-covers/", {"file": image})

        self.assertEqual(response.status_code, 201)
        asset = MediaAsset.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(asset.owner, user)
        self.assertEqual(asset.status, MediaAsset.Status.UPLOADED)
        self.assertEqual(asset.category, MediaAsset.Category.ACTIVITY_COVER)
        upload_stream.assert_called_once()

    @patch("mediafiles.views.delete_public_object")
    @patch("mediafiles.views.build_media_url", return_value="https://media.test/avatar.webp")
    @patch("mediafiles.views.upload_public_stream", return_value="avatar-etag")
    def test_authenticated_user_can_upload_avatar(
        self, upload_stream, _build_url, delete_object
    ):
        user = User.objects.create_user(phone="13900000004")
        old_asset = MediaAsset.objects.create(
            owner=user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.AVATAR,
            status=MediaAsset.Status.UPLOADED,
            object_key="public/avatars/old.webp",
        )
        user.avatar_object_key = old_asset.object_key
        user.save(update_fields=("avatar_object_key",))
        self.client.force_login(user)
        image = SimpleUploadedFile("avatar.webp", self.valid_webp(), content_type="image/webp")

        response = self.client.post("/api/v1/media/avatars/", {"file": image})

        self.assertEqual(response.status_code, 201)
        user.refresh_from_db()
        asset = MediaAsset.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(asset.category, MediaAsset.Category.AVATAR)
        self.assertEqual(user.avatar_object_key, asset.object_key)
        old_asset.refresh_from_db()
        self.assertEqual(old_asset.status, MediaAsset.Status.DELETED)
        upload_stream.assert_called_once()
        delete_object.assert_called_once_with(object_key="public/avatars/old.webp")

    def test_activity_cover_rejects_unsupported_file_type(self):
        user = User.objects.create_user(phone="13900000002")
        self.client.force_login(user)
        document = SimpleUploadedFile("cover.txt", b"not-an-image", content_type="text/plain")

        response = self.client.post("/api/v1/media/activity-covers/", {"file": document})

        self.assertEqual(response.status_code, 400)

    @patch("mediafiles.views.upload_public_stream")
    def test_activity_cover_rejects_corrupt_image_content(self, upload_stream):
        user = User.objects.create_user(phone="13900000003")
        self.client.force_login(user)
        image = SimpleUploadedFile(
            "cover.webp", b"not-really-an-image", content_type="image/webp"
        )

        response = self.client.post("/api/v1/media/activity-covers/", {"file": image})

        self.assertEqual(response.status_code, 400)
        upload_stream.assert_not_called()
