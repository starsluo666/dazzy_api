from unittest.mock import patch

from django.test import TestCase

from accounts.models import User

from .models import MediaAsset


class MediaAssetTests(TestCase):
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
