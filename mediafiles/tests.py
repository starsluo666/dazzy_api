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
