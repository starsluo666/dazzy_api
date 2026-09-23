import uuid
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from mediafiles.models import MediaAsset
from .media import gallery_assets
from .models import (
    ProviderCategoryGrant,
    ProviderProfile,
    ProviderProfileMedia,
    ProviderProfileRevision,
    ServiceCategory,
)
from .serializers import ProviderDetailSerializer, ProviderListItemSerializer


class ProviderGalleryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(phone="13966000001", nickname="测试达人")
        cls.other = User.objects.create_user(phone="13966000002")
        cls.admin = User.objects.create_superuser(phone="13966000003", password="test-password")
        cls.photo = cls.asset("old-cover")
        cls.photo2 = cls.asset("new-cover")
        cls.video = cls.asset("video", category=MediaAsset.Category.PROVIDER_VIDEO)
        cls.profile = ProviderProfile.objects.create(
            user=cls.user,
            status="approved",
            onboarding_status="approved",
            identity_status="verified",
            display_name="测试达人",
            bio="这是一段已经通过审核的达人个人介绍。",
            lifestyle_photo=cls.photo,
            service_city_code="130400",
            service_city_name="邯郸市",
            rating="4.75",
            credit_score=93,
        )

    @classmethod
    def asset(cls, key, **kwargs):
        fields = dict(
            owner=cls.user,
            scope="public",
            status="uploaded",
            category="provider_photo",
            object_key=f"test/gallery/{key}",
        )
        fields.update(kwargs)
        return MediaAsset.objects.create(**fields)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def submit(self, ids):
        return self.client.patch(
            "/api/v1/providers/me/profile/", {"media_ids": [str(pk) for pk in ids]}, format="json"
        )

    def review(self, decision):
        revision = ProviderProfileRevision.objects.get(provider=self.profile, status="pending")
        self.client.force_authenticate(self.admin)
        result = self.client.post(
            reverse("backoffice-provider-change-review-action", args=("profile", revision.pk)),
            {
                "decision": decision,
                "reason": "请补充清晰的本人照片" if decision == "reject" else "",
            },
            format="json",
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.profile.refresh_from_db()

    @patch("providers.media.build_media_url", side_effect=lambda key: f"https://test/{key}")
    def test_gallery_is_pending_until_approved_and_list_always_uses_first_photo(self, _url):
        response = self.submit([self.photo2.pk, self.video.pk, self.photo.pk])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [item["type"] for item in response.data["data"]["media"]], ["image", "video", "image"]
        )
        self.assertEqual(response.data["data"]["lifestyle_photo_id"], self.photo2.pk)
        self.profile.refresh_from_db()
        self.assertEqual([asset.pk for asset in gallery_assets(self.profile)], [self.photo.pk])
        own = self.client.get("/api/v1/providers/me/profile/")
        self.assertEqual(own.data["data"]["media"][0]["id"], str(self.photo2.pk))
        self.review("approve")
        self.assertEqual(
            [asset.pk for asset in gallery_assets(self.profile)],
            [self.photo2.pk, self.video.pk, self.photo.pk],
        )
        self.assertEqual(self.profile.lifestyle_photo_id, self.photo2.pk)
        with patch(
            "providers.serializers.build_media_url", side_effect=lambda key: f"https://test/{key}"
        ):
            self.assertEqual(
                ProviderListItemSerializer(self.profile).data["avatar_url"],
                f"https://test/{self.photo2.object_key}",
            )
            self.assertEqual(
                ProviderDetailSerializer(self.profile).data["media"][1]["type"], "video"
            )

    def test_rejection_does_not_replace_gallery(self):
        self.assertEqual(self.submit([self.photo2.pk, self.video.pk]).status_code, 200)
        self.review("reject")
        self.assertEqual([asset.pk for asset in gallery_assets(self.profile)], [self.photo.pk])
        self.assertEqual(self.profile.lifestyle_photo_id, self.photo.pk)
        self.client.force_authenticate(self.user)
        self.assertEqual(
            self.client.get("/api/v1/providers/me/profile/").data["data"]["review_status"],
            "rejected",
        )

    def test_initial_combined_approval_publishes_full_gallery(self):
        self.profile.onboarding_status = "incomplete"
        self.profile.save(update_fields=("onboarding_status",))
        category = ServiceCategory.objects.create(name="展示测试服务", slug="gallery-test")
        ProviderCategoryGrant.objects.create(
            provider=self.profile, category=category, granted_by=self.admin
        )
        response = self.submit([self.photo2.pk, self.video.pk])
        self.assertEqual(response.status_code, 200, response.data)
        service = self.client.post(
            "/api/v1/providers/me/services/",
            {"category_id": category.pk, "billing_type": "hourly", "price_amount": 10000},
            format="json",
        )
        self.assertEqual(service.status_code, 201, service.data)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.onboarding_status, "pending_review")
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse(
                "backoffice-provider-change-review-action", args=("onboarding", self.profile.pk)
            ),
            {"decision": "approve"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.onboarding_status, "approved")
        self.assertEqual(
            [asset.pk for asset in gallery_assets(self.profile)], [self.photo2.pk, self.video.pk]
        )

    def test_approval_rechecks_asset_status_and_rolls_back_changes(self):
        self.assertEqual(self.submit([self.photo2.pk, self.video.pk]).status_code, 200)
        self.video.status = "rejected"
        self.video.save(update_fields=("status",))
        self.client.force_authenticate(self.admin)
        revision = self.profile.profile_revisions.get(status="pending")
        response = self.client.post(
            reverse("backoffice-provider-change-review-action", args=("profile", revision.pk)),
            {"decision": "approve"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.profile.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(self.profile.lifestyle_photo_id, self.photo.pk)
        self.assertEqual(revision.status, "pending")

    def test_rejects_wrong_owner_private_pending_and_unrelated_assets(self):
        invalid = [
            self.asset("foreign", owner=self.other),
            self.asset("private", scope="private"),
            self.asset("pending", status="pending"),
            self.asset("avatar", category="avatar"),
            self.asset("deleted", status="deleted"),
        ]
        for item in invalid:
            with self.subTest(asset=item.object_key):
                self.assertEqual(self.submit([self.photo.pk, item.pk]).status_code, 400)
        self.assertFalse(self.profile.profile_revisions.exists())

    def test_rejects_empty_duplicate_missing_and_video_cover(self):
        for ids in (
            [],
            [self.photo.pk, self.photo.pk],
            [uuid.uuid4()],
            [self.video.pk, self.photo.pk],
        ):
            with self.subTest(ids=ids):
                self.assertEqual(self.submit(ids).status_code, 400)

    def test_enforces_gallery_and_video_limits(self):
        photos = [self.asset(f"limit-photo-{i}") for i in range(10)]
        self.assertEqual(self.submit([item.pk for item in photos]).status_code, 400)
        videos = [self.asset(f"limit-video-{i}", category="provider_video") for i in range(4)]
        self.assertEqual(
            self.submit([self.photo.pk] + [item.pk for item in videos]).status_code, 400
        )
        self.assertEqual(
            self.submit(
                [item.pk for item in photos[:6]] + [item.pk for item in videos[:3]]
            ).status_code,
            200,
        )

    def test_legacy_edit_keeps_additional_media(self):
        for position, asset in enumerate([self.photo, self.video]):
            ProviderProfileMedia.objects.create(
                provider=self.profile, asset=asset, position=position
            )
        response = self.client.patch(
            "/api/v1/providers/me/profile/",
            {"lifestyle_photo_id": str(self.photo2.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [item["id"] for item in response.data["data"]["media"]],
            [str(self.photo2.pk), str(self.video.pk)],
        )

    def test_pending_revision_cannot_be_overwritten(self):
        self.assertEqual(self.submit([self.photo2.pk, self.video.pk]).status_code, 200)
        self.assertEqual(self.submit([self.photo.pk]).status_code, 400)

    def test_admin_gallery_only_loads_for_authorized_detail(self):
        from backoffice.serializers import ProviderAdminSerializer

        for context in ({}, {"can_review": True}, {"include_detail": True}):
            with self.assertNumQueries(0):
                self.assertEqual(
                    ProviderAdminSerializer(context=context).get_media(self.profile), []
                )
        media = ProviderAdminSerializer(
            context={"can_review": True, "include_detail": True}
        ).get_media(self.profile)
        self.assertEqual(media[0]["id"], str(self.photo.pk))

    def test_migration_backfills_legacy_profile_and_pending_revision_once(self):
        from .models import ProviderProfileRevisionMedia

        revision = ProviderProfileRevision.objects.create(
            provider=self.profile,
            display_name="旧资料修订",
            bio=self.profile.bio,
            lifestyle_photo=self.photo2,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        migration = import_module("providers.migrations.0018_backfill_profile_galleries")
        migration.backfill(apps, SimpleNamespace(connection=connection))
        migration.backfill(apps, SimpleNamespace(connection=connection))
        self.assertEqual(
            list(self.profile.gallery_items.values_list("asset_id", "position")),
            [(self.photo.pk, 0)],
        )
        self.assertEqual(
            list(
                ProviderProfileRevisionMedia.objects.filter(revision=revision).values_list(
                    "asset_id", "position"
                )
            ),
            [(self.photo2.pk, 0)],
        )

    def test_credit_and_rating_are_from_current_provider(self):
        workbench = self.client.get("/api/v1/providers/me/workbench/")
        self.assertEqual(workbench.status_code, 200, workbench.data)
        self.assertEqual(workbench.data["data"]["rating"], "4.75")
        self.assertEqual(workbench.data["data"]["credit_score"], 93)
        self.assertEqual(
            self.client.get("/api/v1/auth/security/").data["data"]["provider_credit_score"], 93
        )
        self.client.force_authenticate(self.other)
        self.assertIsNone(
            self.client.get("/api/v1/auth/security/").data["data"]["provider_credit_score"]
        )
