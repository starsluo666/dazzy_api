from importlib import import_module
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from mediafiles.models import MediaAsset
from moderation import services
from moderation.models import ContentReview
from .models import User
from .nicknames import validate_nickname


class NicknameWordTests(SimpleTestCase):
    def test_safe_names_keep_original_typography(self):
        for value in ("小雨", "用户1234", "慢慢来😊", "乐搭伴用户", "Ａｌｉｃｅ", "Alice_2026"):
            self.assertEqual(validate_nickname(f" {value} "), value)

    def test_sensitive_words_are_rejected_even_with_separators_or_full_width(self):
        for value in ("操你妈", "操 你 妈", "操\u200b你妈", "傻-Ｂ", "ＦＵＣＫ", "裸.聊", "官方客服123", "加ＶＸ小张"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_nickname(value)

    @override_settings(NICKNAME_EXTRA_BLOCKED_WORDS=("测试违规词", "", "MYSPAM"))
    def test_operator_extra_words_are_normalized_and_added(self):
        for value in ("测试-违规词", "ｍｙ ｓｐａｍ"):
            with self.assertRaises(ValidationError):
                validate_nickname(value)
        self.assertEqual(validate_nickname("我的昵称"), "我的昵称")

    def test_invisible_only_names_are_rejected(self):
        for value in (" ", "\u200b\u200d", "\x00"):
            with self.assertRaises(ValidationError):
                validate_nickname(value)


@override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=True,
                   TENCENT_CONTENT_REVIEW_SECRET_ID="offline-id", TENCENT_CONTENT_REVIEW_SECRET_KEY="offline-key")
class NicknameProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(phone="13800005555", nickname="原昵称")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No live network"))
        network.start()
        self.addCleanup(network.stop)

    def change(self, value):
        return self.client.patch("/api/v1/users/me/", {"nickname": value}, format="json")

    def test_safe_nickname_waits_for_worker_before_publishing(self):
        with patch("moderation.gateway.detect_text") as gateway:
            response = self.change(" 用户新昵称 ")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["nickname"], "原昵称")
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "原昵称")
        self.assertEqual(ContentReview.objects.get(kind="nickname").status, "queued")
        gateway.assert_not_called()

    def test_flagged_name_is_not_immediately_published(self):
        response = self.change("平台客服123")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "原昵称")
        self.assertTrue(ContentReview.objects.get().local_findings)

    def test_old_review_cannot_overwrite_latest_name_even_when_old_base_returns(self):
        old = services.submit("nickname", self.user, candidate="旧待审核昵称")
        original_revision = old.revision
        for nickname in ("新昵称", "原昵称"):
            self.assertEqual(self.change(nickname).status_code, 200)
        old.refresh_from_db()
        self.assertFalse(old.is_current)
        self.assertEqual(old.status, "superseded")
        with self.assertRaises(ValidationError):
            services.decide(old.pk, actor=self.user, action="approve", revision=original_revision, reason="旧结果不应再生效")
        self.assertIsNone(services._claim(old.pk))
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "原昵称")

    def test_profile_status_exposes_both_pending_changes(self):
        asset = MediaAsset.objects.create(
            owner=self.user, scope="public", category="avatar", status="uploaded", object_key="test-avatar.webp",
        )
        services.submit("avatar", self.user, candidate=asset.pk)
        services.submit("nickname", self.user, candidate="旧昵称请求")
        response = self.client.get("/api/v1/users/me/")
        self.assertEqual({row["kind"] for row in response.data["data"]["content_reviews"]}, {"avatar", "nickname"})

    def test_admin_registration_cannot_enqueue_a_nickname(self):
        admin = User.objects.create_superuser(phone="13800005556", password="test-password")
        self.client.force_authenticate(admin)
        response = self.client.post("/api/v1/admin/content-reviews/", {"kind": "nickname", "target_id": self.user.pk}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ContentReview.objects.exists())

    def test_upgrade_retires_only_pending_nickname_work(self):
        old = services.submit("nickname", self.user, candidate="旧申请昵称")
        approved_user = User.objects.create_user(phone="13800005557", nickname="已通过昵称")
        approved = services.submit("nickname", approved_user, candidate="已通过昵称")
        approved.status = "approved"
        approved.save(update_fields=("status",))
        migration = import_module("moderation.migrations.0002_retire_pending_nickname_reviews")
        migration.retire_pending_nicknames(apps, SimpleNamespace(connection=connection))
        old.refresh_from_db()
        approved.refresh_from_db()
        self.assertFalse(old.is_current)
        self.assertEqual(old.status, "superseded")
        self.assertIsNone(old.lease_token)
        self.assertTrue(approved.is_current)
        self.assertEqual(approved.status, "approved")
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "原昵称")

    def test_operator_can_resolve_a_local_rule_match(self):
        old = services.submit("nickname", self.user, candidate="平台客服")
        self.assertTrue(old.local_findings)
        admin = User.objects.create_superuser(phone="13800005559", password="test-password")
        services.decide(old.pk, actor=admin, action="approve", revision=old.revision, reason="已核实本地初筛警报")
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "平台客服")

    def test_manager_defaults_to_phone_suffix_and_preserves_explicit_name(self):
        user = User.objects.create_user(phone="13800006666")
        self.assertEqual(user.nickname, "用户6666")
        explicit = User.objects.create_user(phone="13800006667", nickname="自定义名字")
        self.assertEqual(explicit.nickname, "自定义名字")


@override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=True,
                   TENCENT_CONTENT_REVIEW_SECRET_ID="offline-id", TENCENT_CONTENT_REVIEW_SECRET_KEY="offline-key")
class NicknameConcurrencyTests(TransactionTestCase):
    def test_late_detector_cannot_publish_an_older_nickname(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL independent connections")
        user = User.objects.create_user(phone="13800005558", nickname="原昵称")
        review = services.submit("nickname", user, candidate="旧请求昵称")
        entered, release = Event(), Event()

        def detect(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError("detector not released")
            return {"suggestion": "Pass", "request_id": "synthetic-pass", "label": "Normal", "score": 0}

        def worker():
            close_old_connections()
            try:
                return services.process_one(review.pk)
            finally:
                close_old_connections()

        with patch("moderation.gateway.detect_text", side_effect=detect), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(10))
                client = APIClient()
                client.force_authenticate(user)
                response = client.patch("/api/v1/users/me/", {"nickname": "最新待审昵称"}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
            finally:
                release.set()
            result.result(timeout=10)
        review.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(user.nickname, "原昵称")
        self.assertEqual(ContentReview.objects.get(kind="nickname", is_current=True).snapshot["values"]["nickname"], "最新待审昵称")
        self.assertEqual(review.status, "superseded")
        self.assertFalse(review.is_current)
