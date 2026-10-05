from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from accounts.models import User
from activities.models import ActivityCategory
from mediafiles.models import MediaAsset
from providers.models import ServiceCategory

from .models import AdminAuditLog
from .serializers import AdminActivityCategorySerializer, AdminServiceCategorySerializer


class OperationsAssetTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(phone="19900002221", password="test-password")
        cls.customer = User.objects.create_user(phone="19900002222", password="test-password")

    def setUp(self):
        self.client.force_authenticate(self.admin)

    @staticmethod
    def png_file():
        body = BytesIO()
        Image.new("RGB", (64, 64), "#00aeb4").save(body, format="PNG")
        return SimpleUploadedFile("category.png", body.getvalue(), content_type="application/octet-stream")

    def make_asset(self, name="icon.png", *, status=MediaAsset.Status.UPLOADED):
        return MediaAsset.objects.create(
            owner=self.admin, scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.OPERATIONS_ICON, status=status,
            object_key=f"dazzy-test/public/operations/icons/{name}", original_filename=name,
            content_type="image/png", size_bytes=100,
        )

    @patch("backoffice.asset_views.build_media_url", return_value="https://example.test/photo.jpg")
    def test_operations_assets_accept_heic_and_store_jpeg(self, _url):
        from mediafiles.test_heif_uploads import heif_photo

        stored = []
        def capture(**kwargs):
            stored.append(kwargs["body"].read())
            return "etag"

        with patch("mediafiles.views.upload_public_stream", side_effect=capture):
            for kind in ("image", "icon"):
                response = self.client.post(reverse("backoffice-assets"), {
                    "file": SimpleUploadedFile("IMG.HEIC", heif_photo(), "application/octet-stream"),
                    "kind": kind,
                }, format="multipart")
                self.assertEqual(response.status_code, 201, response.data)
                asset = MediaAsset.objects.get(pk=response.data["data"]["id"])
                self.assertEqual(asset.content_type, "image/jpeg")
                self.assertTrue(asset.object_key.endswith(".jpg"))
                with Image.open(BytesIO(stored[-1])) as decoded:
                    self.assertEqual(decoded.format, "JPEG")

    @patch("backoffice.asset_views.build_media_url", return_value="https://example.test/icon.png")
    @patch("mediafiles.views.upload_public_stream", return_value="etag")
    def test_upload_and_list_only_curated_public_assets(self, upload_stream, _url):
        response = self.client.post(
            reverse("backoffice-assets"), {"file": self.png_file(), "kind": "icon"}, format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        asset = MediaAsset.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(asset.category, MediaAsset.Category.OPERATIONS_ICON)
        self.assertEqual(asset.content_type, "image/png")
        self.assertTrue(asset.object_key.startswith("dazzy-test/public/operations/icons/"))
        upload_stream.assert_called_once()
        self.assertEqual(upload_stream.call_args.kwargs["content_type"], "image/png")
        MediaAsset.objects.create(
            owner=self.customer, scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.IDENTITY, status=MediaAsset.Status.UPLOADED,
            object_key="dazzy-test/private/identity/private.jpg",
        )
        listed = self.client.get(reverse("backoffice-assets"))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["data"]["pagination"]["total"], 1)
        self.assertEqual(listed.data["data"]["items"][0]["id"], str(asset.pk))
        self.assertTrue(AdminAuditLog.objects.filter(action="operations_asset.upload").exists())

    @patch("backoffice.serializers.build_media_url", return_value="https://example.test/icon.png")
    def test_categories_select_asset_by_id_and_reject_private_asset(self, _url):
        asset = self.make_asset()
        service = self.client.post(reverse("backoffice-service-categories"), {
            "name": "桌球", "slug": "billiards-test", "icon_asset_id": str(asset.pk),
        }, format="json")
        self.assertEqual(service.status_code, 201)
        self.assertEqual(service.data["data"]["icon_asset_id"], asset.pk)
        service_category = ServiceCategory.objects.get(pk=service.data["data"]["id"])
        self.assertEqual(service_category.icon_asset_id, asset.pk)
        self.assertEqual(service_category.icon_object_key, asset.object_key)

        activity = self.client.post(reverse("backoffice-activity-categories"), {
            "name": "桌球活动", "slug": "billiards-activity-test", "icon_asset_id": str(asset.pk),
        }, format="json")
        self.assertEqual(activity.status_code, 201)
        self.assertEqual(ActivityCategory.objects.get(pk=activity.data["data"]["id"]).icon_asset_id, asset.pk)

        private = MediaAsset.objects.create(
            owner=self.customer, scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.IDENTITY, status=MediaAsset.Status.UPLOADED,
            object_key="dazzy-test/private/identity/secret.png",
        )
        denied = self.client.patch(
            reverse("backoffice-service-category-detail", args=(service_category.pk,)),
            {"icon_asset_id": str(private.pk)}, format="json",
        )
        self.assertEqual(denied.status_code, 400)

    @patch("backoffice.asset_views.build_media_url", return_value="https://example.test/icon.png")
    def test_batch_delete_blocks_references_and_restores_unreferenced_asset(self, _url):
        in_use = self.make_asset("used.png")
        unused = self.make_asset("unused.png")
        ServiceCategory.objects.create(
            name="棋牌", slug="board-games-test", icon_asset=in_use,
            icon_object_key=in_use.object_key,
        )
        response = self.client.post(reverse("backoffice-assets-batch-delete"), {
            "ids": [str(in_use.pk), str(unused.pk)],
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["deleted"], [str(unused.pk)])
        self.assertEqual(response.data["data"]["blocked"][0]["id"], str(in_use.pk))
        in_use.refresh_from_db()
        unused.refresh_from_db()
        self.assertEqual(in_use.status, MediaAsset.Status.UPLOADED)
        self.assertEqual(unused.status, MediaAsset.Status.DELETED)
        self.assertEqual(self.client.get(reverse("backoffice-assets")).data["data"]["pagination"]["total"], 1)
        self.assertEqual(self.client.get(reverse("backoffice-assets"), {"status": "trash"}).data["data"]["pagination"]["total"], 1)
        restored = self.client.post(reverse("backoffice-assets-restore", args=(unused.pk,)))
        self.assertEqual(restored.status_code, 200)
        unused.refresh_from_db()
        self.assertEqual(unused.status, MediaAsset.Status.UPLOADED)

    def test_customer_cannot_see_assets(self):
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.client.get(reverse("backoffice-assets")).status_code, 403)

    @patch("backoffice.asset_views.build_media_url", return_value="https://example.test/icon.png")
    def test_legacy_object_key_also_prevents_deletion(self, _url):
        asset = self.make_asset("legacy.png")
        ActivityCategory.objects.create(
            name="桌游", slug="board-games-legacy", icon_object_key=asset.object_key,
        )
        response = self.client.post(
            reverse("backoffice-assets-batch-delete"), {"ids": [str(asset.pk)]}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["deleted"], [])
        self.assertEqual(response.data["data"]["blocked"][0]["references"][0]["type"], "activity_category")

    def test_deleted_asset_cannot_be_selected_as_icon(self):
        asset = self.make_asset("deleted.png", status=MediaAsset.Status.DELETED)
        response = self.client.post(reverse("backoffice-service-categories"), {
            "name": "密室", "slug": "escape-room-deleted", "icon_asset_id": str(asset.pk),
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_category_creation_rechecks_asset_deleted_after_validation(self):
        for model, serializer_class in (
            (ServiceCategory, AdminServiceCategorySerializer),
            (ActivityCategory, AdminActivityCategorySerializer),
        ):
            with self.subTest(model=model.__name__):
                asset = self.make_asset(f"create-{model.__name__}.png")
                serializer = serializer_class(data={
                    "name": "新分类", "slug": "concurrent-create", "icon_asset_id": str(asset.pk),
                })
                serializer.is_valid(raise_exception=True)
                MediaAsset.objects.filter(pk=asset.pk).update(status=MediaAsset.Status.DELETED)
                with self.assertRaises(ValidationError):
                    serializer.save()
                self.assertFalse(model.objects.filter(slug="concurrent-create").exists())

    def test_category_update_rechecks_asset_and_preserves_previous_icon(self):
        for model, serializer_class in (
            (ServiceCategory, AdminServiceCategorySerializer),
            (ActivityCategory, AdminActivityCategorySerializer),
        ):
            with self.subTest(model=model.__name__):
                previous = self.make_asset(f"old-{model.__name__}.png")
                replacement = self.make_asset(f"new-{model.__name__}.png")
                category = model.objects.create(
                    name="原分类", slug="concurrent-update", icon_asset=previous,
                    icon_object_key=previous.object_key,
                )
                serializer = serializer_class(category, data={
                    "icon_asset_id": str(replacement.pk),
                }, partial=True)
                serializer.is_valid(raise_exception=True)
                MediaAsset.objects.filter(pk=replacement.pk).update(status=MediaAsset.Status.DELETED)
                with self.assertRaises(ValidationError):
                    serializer.save()
                category.refresh_from_db()
                self.assertEqual(category.icon_asset_id, previous.pk)
                self.assertEqual(category.icon_object_key, previous.object_key)
