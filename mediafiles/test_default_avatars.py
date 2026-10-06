from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from PIL import Image

from accounts.models import User
from .default_avatars import DEFAULT_AVATAR_NAMES, choose_default_avatar, default_avatar_keys
from .models import MediaAsset


@override_settings(COS_PUBLIC_PREFIX="avatar-test/public/")
class DefaultAvatarTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            phone="13977005100", password=None, avatar_object_key="",
        )

    def publish(self, **kwargs):
        call_command("publish_default_avatars", stdout=StringIO(), **kwargs)

    def asset(self, index=0, **kwargs):
        values = {
            "owner": self.owner, "object_key": default_avatar_keys()[index],
            "scope": MediaAsset.Scope.PUBLIC,
            "category": MediaAsset.Category.OPERATIONS_IMAGE,
            "status": MediaAsset.Status.UPLOADED,
        }
        values.update(kwargs)
        return MediaAsset.objects.create(**values)

    def test_empty_pool_keeps_placeholder(self):
        user = User.objects.create_user(phone="13977005101")
        self.assertEqual(user.avatar_object_key, "")

    def test_assignment_is_random_once_and_persisted(self):
        keys = default_avatar_keys()
        for index in range(len(keys)):
            self.asset(index)
        with patch("mediafiles.default_avatars.choice", side_effect=keys) as choose:
            users = [User.objects.create_user(phone=f"1397700520{i}") for i in range(6)]
        self.assertEqual({user.avatar_object_key for user in users}, set(keys))
        for user in users:
            stored = user.avatar_object_key
            user.nickname = "新昵称"
            user.save(update_fields=("nickname",))
            user.refresh_from_db()
            self.assertEqual(user.avatar_object_key, stored)
        self.assertEqual(choose.call_count, 6)
        self.assertEqual(set(choose.call_args.args[0]), set(keys))

    def test_preserves_explicit_avatar_and_blank(self):
        self.asset()
        with patch("mediafiles.default_avatars.choice") as choose:
            for index, key in enumerate(("custom/avatar.jpg", "")):
                user = User.objects.create_user(phone=f"1397700530{index}", avatar_object_key=key)
                self.assertEqual(user.avatar_object_key, key)
        choose.assert_not_called()

    def test_pool_only_includes_published_public_system_assets(self):
        self.asset(0, status=MediaAsset.Status.PENDING)
        self.asset(1, status=MediaAsset.Status.DELETED)
        self.asset(2, scope=MediaAsset.Scope.PRIVATE)
        self.asset(3, category=MediaAsset.Category.AVATAR)
        self.asset(4, object_key="avatar-test/public/operations/random.webp")
        ready = self.asset(5)
        self.assertEqual(choose_default_avatar(), ready.object_key)

    @patch("mediafiles.management.commands.publish_default_avatars.upload_public_file", return_value="etag")
    def test_publication_is_idempotent_and_does_not_backfill(self, upload):
        old_user = User.objects.create_user(phone="13977005401")
        self.publish()
        self.assertEqual(upload.call_count, 6)
        self.assertEqual(MediaAsset.objects.count(), 6)
        self.publish()
        self.assertEqual(upload.call_count, 6)
        old_user.refresh_from_db()
        self.assertEqual(old_user.avatar_object_key, "")
        user = User.objects.create_user(phone="13977005402")
        self.assertIn(user.avatar_object_key, default_avatar_keys())

    @patch("mediafiles.management.commands.publish_default_avatars.upload_public_file")
    def test_dry_run_has_no_side_effects(self, upload):
        self.publish(dry_run=True)
        upload.assert_not_called()
        self.assertEqual(MediaAsset.objects.count(), 0)

    @patch("mediafiles.management.commands.publish_default_avatars.upload_public_file")
    def test_failed_upload_does_not_publish_partial_pool(self, upload):
        upload.side_effect = ["etag", OSError("COS unavailable")]
        with self.assertRaises(OSError):
            self.publish()
        self.assertEqual(MediaAsset.objects.count(), 0)
        self.assertEqual(choose_default_avatar(), "")

    @patch("mediafiles.management.commands.publish_default_avatars.upload_public_file")
    def test_conflicting_version_cannot_be_overwritten(self, upload):
        self.asset(checksum_sha256="different-content")
        with self.assertRaises(CommandError):
            self.publish()
        upload.assert_not_called()

    @patch("mediafiles.management.commands.publish_default_avatars.upload_public_file")
    def test_requires_active_admin(self, upload):
        self.owner.is_active = False
        self.owner.save(update_fields=("is_active",))
        with self.assertRaises(CommandError):
            self.publish()
        upload.assert_not_called()

    def test_system_assets_are_protected_in_library(self):
        from backoffice.asset_views import asset_references

        refs = asset_references(self.asset())
        self.assertEqual(refs[0]["type"], "default_avatar")

    def test_library_delete_api_rejects_shared_default_avatar(self):
        from django.urls import reverse
        from rest_framework.test import APIClient

        shared = self.asset()
        client = APIClient()
        client.force_authenticate(self.owner)
        response = client.post(
            reverse("backoffice-assets-batch-delete"), {"ids": [str(shared.pk)]}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["deleted"], [])
        self.assertEqual(response.data["data"]["blocked"][0]["id"], str(shared.pk))
        self.assertEqual(
            response.data["data"]["blocked"][0]["references"][0]["type"], "default_avatar",
        )
        shared.refresh_from_db()
        self.assertEqual(shared.status, MediaAsset.Status.UPLOADED)

    @patch("mediafiles.views.build_media_url", return_value="https://example.test/custom.webp")
    @patch("mediafiles.views.delete_public_object")
    def test_replacing_avatar_never_deletes_shared_file(self, delete, _url):
        from mediafiles.views import AvatarUploadView

        shared = self.asset()
        first = User.objects.create_user(phone="13977005501")
        other = User.objects.create_user(phone="13977005502")
        custom = self.asset(object_key="custom/first.webp", owner=first, category=MediaAsset.Category.AVATAR)
        with patch.object(AvatarUploadView, "create_asset", return_value=custom):
            response = AvatarUploadView().post(SimpleNamespace(user=first))
        self.assertEqual(response.status_code, 201)
        delete.assert_not_called()
        first.refresh_from_db()
        other.refresh_from_db()
        shared.refresh_from_db()
        self.assertEqual(first.avatar_object_key, custom.object_key)
        self.assertEqual(other.avatar_object_key, shared.object_key)
        self.assertEqual(shared.status, MediaAsset.Status.UPLOADED)

    def test_packaged_images_are_small_square_webp(self):
        for name in DEFAULT_AVATAR_NAMES:
            path = Path(settings.BASE_DIR) / "assets" / "default-avatars" / "v1" / f"{name}.webp"
            with Image.open(path) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (256, 256))
                self.assertNotIn("A", image.getbands())
                image.verify()
            self.assertLess(path.stat().st_size, 30 * 1024)

    @patch("accounts.serializers.verify_sms_code")
    def test_phone_registration_uses_pool(self, _verify):
        from accounts.serializers import RegisterSerializer

        key = self.asset().object_key
        user = RegisterSerializer().create({
            "phone": "13977005601", "password": "test-only-password", "code": "test-only",
        })
        self.assertEqual(user.avatar_object_key, key)

    def test_miniprogram_registration_and_repeat_login(self):
        from accounts.serializers import WechatMiniProgramLoginSerializer

        key = self.asset().object_key
        serializer = WechatMiniProgramLoginSerializer()
        serializer._validated_data = {
            "wechat_config": SimpleNamespace(app_id="wx-default-avatar-test"),
            "wechat_openid": "avatar-openid", "wechat_unionid": "",
            "wechat_phone": "13977005602",
        }
        user = serializer.save()
        self.assertTrue(serializer.created_new_user)
        self.assertEqual(user.avatar_object_key, key)
        user.avatar_object_key = "custom/changed.jpg"
        user.save(update_fields=("avatar_object_key",))
        self.assertEqual(serializer.save().avatar_object_key, "custom/changed.jpg")
        self.assertFalse(serializer.created_new_user)

    @patch("growth.services.register_invited_user")
    @patch("accounts.wechat_login._attach_identity")
    @patch("accounts.wechat_login._consume_ticket")
    @patch("accounts.wechat_login.load_ticket")
    @patch("accounts.services.verify_sms_code")
    def test_wechat_phone_binding_new_user_uses_pool(self, *_mocks):
        from accounts.wechat_login import bind_phone

        key = self.asset().object_key
        user, created = bind_phone(ticket="mock-ticket", phone="13977005603", code="test-only")
        self.assertTrue(created)
        self.assertEqual(user.avatar_object_key, key)

    def test_historical_manager_does_not_query_media_schema(self):
        from django.apps import apps
        from django.db.migrations.state import ProjectState

        state = ProjectState.from_apps(apps)
        # A historical manager must not depend on tables introduced later.
        historical = state.apps.get_model("accounts", "User")
        with patch("mediafiles.default_avatars.choose_default_avatar") as choose:
            with patch.object(historical, "save"):
                with patch.object(historical, "set_password", create=True):
                    historical.objects.create_user(phone="13977005604")
        choose.assert_not_called()
