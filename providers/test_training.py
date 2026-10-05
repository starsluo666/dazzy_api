import copy
import uuid
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from backoffice.models import AdminAuditLog, AdminRole, Organization, OrganizationMember
from mediafiles.models import MediaAsset
from orders.models import ProviderOrder
from .models import (
    ProviderProfile, ProviderService, ProviderTrainingAttempt, ProviderTrainingConfig,
    ProviderTrainingProgress, ProviderTrainingVersion, ServiceCategory,
)
from .presence import online_provider_query, provider_is_online
from .tests import create_live_location, make_provider_eligible


class FirstOrderTrainingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(phone="13999008001", password="test-password")
        cls.user = User.objects.create_user(phone="13999008002", nickname="学习测试")
        cls.other = User.objects.create_user(phone="13999008003")
        cls.provider = ProviderProfile.objects.create(user=cls.user, status="approved")
        make_provider_eligible(cls.provider)
        cls.provider.training_passed_at = None
        cls.provider.save(update_fields=("training_passed_at",))
        cls.category = ServiceCategory.objects.create(name="测试学习服务", slug="training-test")
        cls.service = ProviderService.objects.create(
            provider=cls.provider, category=cls.category, billing_type="hourly", price_amount=10000,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.curriculum = {
            "title": "首次接单学习", "pass_score": 100,
            "lessons": [{"id": str(uuid.uuid4()), "title": f"规范{i}", "content": "请先联系用户核实订单。"} for i in range(2)],
            "questions": [{"id": str(uuid.uuid4()), "title": f"题目{i}", "options": ["先联系用户", "无需联系"],
                           "correct_index": 0, "explanation": "内部答案依据"} for i in range(2)],
        }

    def save_draft(self, curriculum=None):
        config = self.admin_client.get("/api/v1/admin/provider-training/").data["data"]
        return self.admin_client.patch("/api/v1/admin/provider-training/", {
            "revision": config["revision"], "draft": curriculum or self.curriculum,
        }, format="json")

    def publish(self):
        saved = self.save_draft()
        self.assertEqual(saved.status_code, 200, saved.data)
        published = self.admin_client.post("/api/v1/admin/provider-training/publish/", {
            "revision": saved.data["data"]["revision"],
        }, format="json")
        self.assertEqual(published.status_code, 200, published.data)
        self.version_id = published.data["data"]["published"]["version_id"]
        return published

    def learn(self, lesson_id):
        return self.client.post(f"/api/v1/providers/me/training/lessons/{lesson_id}/complete/", {
            "version_id": self.version_id,
        }, format="json")

    def learn_all(self):
        for lesson in self.curriculum["lessons"]:
            result = self.learn(lesson["id"])
            self.assertEqual(result.status_code, 200, result.data)

    def submit(self, answers=None):
        return self.client.post("/api/v1/providers/me/training/submit/", {
            "version_id": self.version_id,
            "answers": answers if answers is not None else {q["id"]: 0 for q in self.curriculum["questions"]},
        }, format="json")

    def order(self, **changes):
        now = timezone.now()
        values = dict(
            order_no=f"TRAIN{uuid.uuid4().hex[:20]}", customer=self.other, provider=self.provider,
            service=self.service, provider_name_snapshot="测试达人", service_name_snapshot="测试服务",
            billing_type_snapshot="hourly", unit_price_amount=10000, starts_at=now + timedelta(hours=1),
            ends_at=now + timedelta(hours=2), duration_minutes=60, service_fee_amount=10000,
            payable_amount=10000, payment_expires_at=now, paid_at=now,
            acceptance_expires_at=now + timedelta(minutes=30), status="pending_acceptance",
        )
        return ProviderOrder.objects.create(**(values | changes))

    def test_permissions_and_unpublished_state(self):
        for path in ("/api/v1/admin/provider-training/", "/api/v1/admin/provider-training/publish/"):
            self.assertEqual(self.client.post(path, {}, format="json").status_code, 405 if path.endswith("training/") else 403)
        self.assertEqual(self.client.get("/api/v1/admin/provider-training/").status_code, 403)
        self.assertEqual(self.client.patch("/api/v1/admin/provider-training/", {}, format="json").status_code, 403)
        data = self.client.get("/api/v1/providers/me/training/").data["data"]
        self.assertTrue(data["required"])
        self.assertIsNone(data["course"])
        self.version_id = 1
        self.assertEqual(self.submit().status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/providers/me/training/").status_code, 401)
        self.client.force_authenticate(self.other)
        self.assertIn(self.client.get("/api/v1/providers/me/training/").status_code, (400, 403, 404))

    def test_draft_publish_revision_conflict_snapshot_and_audit(self):
        saved = self.save_draft()
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertIsNone(self.client.get("/api/v1/providers/me/training/").data["data"]["course"])
        stale = self.admin_client.patch("/api/v1/admin/provider-training/", {"revision": 0, "draft": self.curriculum}, format="json")
        self.assertEqual(stale.status_code, 409)
        self.publish()
        version = ProviderTrainingVersion.objects.get(pk=self.version_id)
        self.curriculum["title"] = "草稿新标题"
        self.assertEqual(self.save_draft().status_code, 200)
        self.assertEqual(self.client.get("/api/v1/providers/me/training/").data["data"]["course"]["title"], "首次接单学习")
        version.refresh_from_db()
        self.assertEqual(version.payload["title"], "首次接单学习")
        self.assertTrue(AdminAuditLog.objects.filter(action="provider_training.publish", actor=self.admin).exists())
        config = ProviderTrainingConfig.objects.get(pk=1)
        response = self.admin_client.post("/api/v1/admin/provider-training/publish/", {"revision": config.revision - 1}, format="json")
        self.assertEqual(response.status_code, 409)

    def test_only_global_platform_operators_can_manage_training(self):
        organization = Organization.objects.create(name="测试平台", code="training-platform", organization_type="platform")
        role = AdminRole.objects.create(organization=organization, name="学习运营", code="learning", permissions=["operations.manage"], data_scope="all")
        OrganizationMember.objects.create(user=self.other, organization=organization, role=role)
        client = APIClient()
        client.force_authenticate(self.other)
        self.assertEqual(client.get("/api/v1/admin/provider-training/").status_code, 200)
        organization.organization_type = "city_agent"
        organization.save(update_fields=("organization_type",))
        self.assertEqual(client.get("/api/v1/admin/provider-training/").status_code, 403)
        self.assertEqual(client.patch("/api/v1/admin/provider-training/", {}, format="json").status_code, 403)
        self.assertEqual(client.post("/api/v1/admin/provider-training/publish/", {}, format="json").status_code, 403)

    def test_curriculum_validation_and_empty_publish(self):
        invalid = []
        for changes in ({"correct_index": 5}, {"options": ["相同", "相同"]}, {"options": ["只有一个"]}):
            payload = copy.deepcopy(self.curriculum)
            payload["questions"][0].update(changes)
            invalid.append(payload)
        duplicate = copy.deepcopy(self.curriculum)
        duplicate["lessons"][1]["id"] = duplicate["lessons"][0]["id"]
        invalid.append(duplicate)
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.save_draft(payload).status_code, 400)
        for field in ("lessons", "questions"):
            payload = copy.deepcopy(self.curriculum)
            payload[field] = []
            saved = self.save_draft(payload)
            self.assertEqual(saved.status_code, 200)
            result = self.admin_client.post("/api/v1/admin/provider-training/publish/", {"revision": saved.data["data"]["revision"]}, format="json")
            self.assertEqual(result.status_code, 400)
        self.assertFalse(ProviderTrainingVersion.objects.exists())

    def test_no_answers_leak_progress_is_idempotent_and_provider_scoped(self):
        self.publish()
        data = self.client.get("/api/v1/providers/me/training/").data["data"]
        for question in data["course"]["questions"]:
            self.assertEqual(set(question), {"id", "title", "options"})
        lesson_id = self.curriculum["lessons"][0]["id"]
        self.assertEqual(self.learn(lesson_id).status_code, 200)
        self.assertEqual(self.learn(lesson_id).status_code, 200)
        self.assertEqual(ProviderTrainingProgress.objects.get(provider=self.provider).completed_lesson_ids, [lesson_id])
        self.assertEqual(self.learn(uuid.uuid4()).status_code, 400)
        ProviderProfile.objects.create(user=self.other, status="approved")
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/v1/providers/me/training/").data["data"]["completed_lesson_ids"], [])

    def test_all_lessons_required_wrong_answers_retry_and_pass_once(self):
        self.publish()
        self.assertEqual(self.submit().status_code, 400)
        self.learn(self.curriculum["lessons"][0]["id"])
        self.assertEqual(self.submit().status_code, 400)
        self.learn_all()
        failed = self.submit({q["id"]: i for i, q in enumerate(self.curriculum["questions"])})
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.data["data"]["last_result"], {"score": 50, "passed": False})
        self.assertTrue(failed.data["data"]["required"])
        success = self.submit()
        self.assertEqual(success.status_code, 200, success.data)
        self.assertFalse(success.data["data"]["required"])
        self.assertTrue(success.data["data"]["passed"])
        passed_at = success.data["data"]["passed_at"]
        repeated = self.submit()
        self.assertEqual(repeated.data["data"]["passed_at"], passed_at)
        self.assertEqual(ProviderTrainingAttempt.objects.count(), 2)

    def test_invalid_answers_do_not_create_attempts(self):
        self.publish()
        self.learn_all()
        good = {q["id"]: 0 for q in self.curriculum["questions"]}
        qid = next(iter(good))
        for answers in ({}, {qid: 0}, good | {"extra": 0}, *[good | {qid: value} for value in (True, "0", -1, 2, None, {}, [])]):
            with self.subTest(answers=answers):
                self.assertEqual(self.submit(answers).status_code, 400)
        self.assertFalse(ProviderTrainingAttempt.objects.exists())

    def test_configured_pass_score_and_version_updates(self):
        self.curriculum["pass_score"] = 50
        self.publish()
        self.learn_all()
        first_version = self.version_id
        self.curriculum["title"] = "更新资料"
        self.publish()
        latest = self.version_id
        self.version_id = first_version
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(self.learn(self.curriculum["lessons"][0]["id"]).status_code, 409)
        self.version_id = latest
        self.assertEqual(self.submit().status_code, 400)
        self.learn_all()
        result = self.submit({q["id"]: i for i, q in enumerate(self.curriculum["questions"])})
        self.assertTrue(result.data["data"]["passed"])
        self.curriculum["title"] = "再次更新"
        self.publish()
        self.assertFalse(self.client.get("/api/v1/providers/me/training/").data["data"]["required"])

    def test_server_gates_online_and_accept_before_pass(self):
        location = {"longitude": "114.5", "latitude": "36.6", "accuracy_m": "12"}
        result = self.client.post("/api/v1/providers/me/online/start/", location, format="json")
        self.assertEqual(result.status_code, 400, result.data)
        self.assertIn("学习", str(result.data))
        # A stale or manually switched online flag must not bypass public/order checks.
        ProviderProfile.objects.filter(pk=self.provider.pk).update(is_accepting_orders=True)
        create_live_location(self.provider)
        self.provider.refresh_from_db()
        self.assertFalse(provider_is_online(self.provider))
        self.assertFalse(ProviderProfile.objects.filter(online_provider_query(), pk=self.provider.pk).exists())
        order = self.order()
        url = f"/api/v1/providers/me/orders/{order.order_no}/accept/"
        self.assertEqual(self.client.post(url).status_code, 400)
        order.refresh_from_db()
        self.assertIsNone(order.accepted_at)
        self.publish()
        self.learn_all()
        self.submit()
        accepted = self.client.post(url)
        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.assertEqual(accepted.data["data"]["status"], "pending_service")
        self.assertEqual(self.client.post(url).status_code, 200)
        result = self.client.post("/api/v1/providers/me/online/start/", location, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.provider.refresh_from_db()
        self.assertTrue(provider_is_online(self.provider))

    def test_migration_exempts_only_previously_accepted_providers(self):
        new_provider = ProviderProfile.objects.create(user=self.other, status="approved")
        self.order(accepted_at=timezone.now(), status="completed")
        migration = import_module("providers.migrations.0023_first_order_training")
        migration.initialize_training(apps, SimpleNamespace(connection=connection))
        migration.initialize_training(apps, SimpleNamespace(connection=connection))
        self.provider.refresh_from_db()
        new_provider.refresh_from_db()
        self.assertTrue(self.provider.training_exempt)
        self.assertFalse(new_provider.training_exempt)
        data = self.client.get("/api/v1/providers/me/training/").data["data"]
        self.assertFalse(data["required"])
        self.assertTrue(data["exempt"])


class SimplifiedApplicationTests(TestCase):
    def test_application_without_bio_or_radius_and_old_clients_cannot_write_them(self):
        user = User.objects.create_user(phone="13999008004")
        photo = MediaAsset.objects.create(owner=user, category="provider_photo", scope="public", status="uploaded", object_key="test/application.webp")
        client = APIClient()
        client.force_authenticate(user)
        result = client.patch("/api/v1/providers/me/application/", {
            "application_real_name": "测试申请", "application_birth_date": "1998-01-01",
            "lifestyle_photo_id": str(photo.pk), "service_city_code": "130400", "service_city_name": "邯郸市",
        }, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        client.patch("/api/v1/providers/me/application/", {"bio": "旧客户端输入", "max_service_radius_km": 70}, format="json")
        provider = ProviderProfile.objects.get(user=user)
        self.assertEqual(provider.bio, "")
        self.assertEqual(provider.max_service_radius_km, 10)
        submitted = client.post("/api/v1/providers/me/application/submit/", {"agreement_accepted": True}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.assertFalse(provider.is_profile_complete)
