from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.access import resolve_admin_access
from backoffice.models import AdminRole, Organization, OrganizationMember
from orders.models import ProviderOrder
from providers.models import ProviderCategoryGrant
from . import services
from .models import ContentReview as Review
from .tests import fixture, decide, activity


@override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=False)
class ContentBusinessTests(TestCase):
    def setUp(self):
        fixture(self)
        self.access = resolve_admin_access(self.admin)
        self.request = SimpleNamespace(headers={}, META={})
        ProviderCategoryGrant.objects.create(
            provider=self.provider, category=self.category, is_active=True
        )
        self.provider.lifestyle_photo.provider_photo_cropped = True
        self.provider.lifestyle_photo.save()

    def test_service_submission_and_original_business_approval_are_two_separate_gates(self):
        from providers.services import submit_provider_service_revision
        from backoffice.services import _apply_service_revision

        revision = submit_provider_service_revision(
            provider=self.provider, service=self.service, data={"description": "这是新的服务说明"}
        )
        content = Review.objects.get(kind="service_revision", target_id=revision.pk)
        self.assertEqual(content.status, "manual")
        self.service.refresh_from_db()
        self.assertEqual(self.service.description, "公开服务说明")
        with self.assertRaises(ValidationError):
            _apply_service_revision(revision, actor=self.admin, now=timezone.now())
        decide(content, self.admin)
        self.service.refresh_from_db()
        self.assertEqual(self.service.description, "公开服务说明")
        _apply_service_revision(revision, actor=self.admin, now=timezone.now())
        self.service.refresh_from_db()
        self.assertEqual(self.service.description, "这是新的服务说明")

    def test_profile_submission_is_scanned_before_original_approval(self):
        from providers.services import submit_provider_profile_revision
        from backoffice.services import review_provider_profile_revision

        revision = submit_provider_profile_revision(
            provider=self.provider,
            data={"display_name": "新的达人名称", "bio": "新的个人介绍资料保持至少十个字。"},
        )
        content = Review.objects.get(kind="profile_revision", target_id=revision.pk)
        args = dict(
            revision_id=revision.pk,
            decision="approve",
            reason="",
            actor=self.admin,
            access=self.access,
            request=self.request,
        )
        with self.assertRaises(ValidationError):
            review_provider_profile_revision(**args)
        content = decide(content, self.admin)
        self.provider.refresh_from_db()
        self.assertNotEqual(self.provider.display_name, revision.display_name)
        review_provider_profile_revision(**args)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.display_name, revision.display_name)
        decide(content, self.admin, "withdraw")
        self.provider.refresh_from_db()
        self.assertTrue(self.provider.content_hidden)

    def test_rejected_unpublished_revision_can_be_cleared_without_publishing_it(self):
        from providers.services import submit_provider_service_revision

        revision = submit_provider_service_revision(
            provider=self.provider,
            data={
                "category": self.category,
                "billing_type": "per_session",
                "price_amount": 1000,
                "estimated_duration_minutes": 60,
                "description": "新的按次服务",
            },
        )
        content = decide(
            Review.objects.get(kind="service_revision", target_id=revision.pk), self.admin, "reject"
        )
        content = decide(content, self.admin, "approve")
        revision.refresh_from_db()
        self.assertIsNone(revision.service_id)
        self.assertEqual(revision.status, "pending")
        self.assertEqual(content.status, "approved")

    def test_another_record_cannot_steal_or_restore_a_publication_hold(self):
        from providers.services import submit_provider_profile_revision
        from backoffice.services import review_provider_profile_revision

        revision = submit_provider_profile_revision(provider=self.provider, data={})
        old_case = decide(
            Review.objects.get(kind="profile_revision", target_id=revision.pk), self.admin
        )
        review_provider_profile_revision(
            revision_id=revision.pk,
            decision="approve",
            reason="",
            actor=self.admin,
            access=self.access,
            request=self.request,
        )
        self.provider.refresh_from_db()
        canonical = decide(services.submit("provider", self.provider), self.admin)
        canonical = decide(canonical, self.admin, "withdraw")
        old_case = decide(old_case, self.admin, "withdraw")
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.content_hidden_review, canonical.public_id)
        with self.assertRaises(ValidationError):
            decide(old_case, self.admin, "restore")
        with self.assertRaises(ValidationError):
            services.ensure_approved("profile_revision", revision)
        decide(canonical, self.admin, "restore")
        self.provider.refresh_from_db()
        self.assertFalse(self.provider.content_hidden)

    def test_order_review_completion_does_not_wait_for_publication_clearance(self):
        from backoffice.services import moderate_provider_order_review

        now = timezone.now()
        order = ProviderOrder.objects.create(
            order_no="DZY-CONTENT-TEST",
            customer=self.user,
            provider=self.provider,
            service=self.service,
            provider_name_snapshot="测试达人",
            service_name_snapshot="桌球",
            billing_type_snapshot="hourly",
            unit_price_amount=10000,
            starts_at=now - timedelta(hours=3),
            ends_at=now - timedelta(hours=1),
            duration_minutes=120,
            meeting_address="测试地点",
            contact_name="测试",
            contact_phone=self.user.phone,
            service_fee_amount=20000,
            payable_amount=20000,
            payment_expires_at=now,
            status="pending_review",
            review_expires_at=now + timedelta(days=1),
        )
        self.client.force_authenticate(self.user)
        response = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/review/",
            {"rating": 5, "content": "感谢认真服务，非常满意。"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")
        content = Review.objects.get(kind="review", target_id=order.review.pk)
        args = dict(
            order_no=order.order_no,
            action="approve",
            reason="",
            actor=self.admin,
            access=self.access,
            request=self.request,
        )
        with self.assertRaises(ValidationError):
            moderate_provider_order_review(**args)
        content = decide(content, self.admin)
        moderate_provider_order_review(**args)
        order.refresh_from_db()
        self.assertEqual(order.review.audit_status, "approved")
        content = decide(content, self.admin, "withdraw")
        order.refresh_from_db()
        self.assertFalse(order.review.is_visible)
        self.assertEqual(order.status, "completed")
        with self.assertRaises(ValidationError):
            moderate_provider_order_review(**{**args, "action": "restore"})

    def test_new_activity_draft_registers_only_public_content(self):
        from activities.services import create_activity_draft

        obj = activity(self)
        data = {
            field: getattr(obj, field)
            for field in (
                "title",
                "description",
                "participation_rules",
                "starts_at",
                "ends_at",
                "formation_deadline",
                "meeting_place_name",
                "meeting_address",
                "city_code",
                "city_name",
                "capacity",
                "min_participants",
                "aa_principal_amount",
            )
        }
        data.update(longitude=114.5, latitude=36.6, refund_template_version="standard-v1")
        with patch("activities.services.ensure_activity_payment_available"):
            draft = create_activity_draft(organizer=self.user, validated_data=data)
        content = Review.objects.get(kind="activity", target_id=draft.pk)
        self.assertEqual(content.status, "manual")
        self.assertNotIn("phone", content.snapshot["text"])

    def test_disabling_new_submission_gate_does_not_release_existing_takedown(self):
        content = decide(services.submit("provider", self.provider), self.admin)
        decide(content, self.admin, "withdraw")
        with override_settings(CONTENT_REVIEW_ENABLED=False):
            with self.assertRaises(ValidationError):
                services.ensure_approved("provider", self.provider)

    def test_readonly_global_role_can_view_but_city_role_and_readonly_writes_are_denied(self):
        content = services.submit("provider", self.provider)
        organization = Organization.objects.create(
            name="审核团队", code="content-team", organization_type="platform"
        )
        role = AdminRole.objects.create(
            organization=organization,
            name="只读审核",
            code="content-reader",
            data_scope="all",
            permissions=["content.review.view"],
        )
        OrganizationMember.objects.create(user=self.other, organization=organization, role=role)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/v1/admin/content-reviews/").status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/api/v1/admin/content-reviews/{content.pk}/",
                {"action": "approve", "revision": 1, "reason": "已逐项检查内容"},
                format="json",
            ).status_code,
            403,
        )
        role.data_scope = "city"
        role.permissions = ["content.review.view", "content.review.manage"]
        role.save()
        self.assertEqual(self.client.get("/api/v1/admin/content-reviews/").status_code, 403)


@override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=True)
class ContentVersionTests(TransactionTestCase):
    def setUp(self):
        fixture(self)

    def test_old_queue_after_switch_toggle_cannot_overwrite_later_public_edit(self):
        content = services.submit("nickname", self.user, candidate="原等待检测昵称")
        self.user.nickname = "后来人工变更昵称"
        self.user.save()
        with patch(
            "moderation.gateway.detect_text",
            return_value={"suggestion": "Pass", "request_id": "r-1", "label": "Normal", "score": 0},
        ):
            services.process_one(content.pk)
        content.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(content.status, "superseded")
        self.assertEqual(self.user.nickname, "后来人工变更昵称")

    def test_avatar_withdraw_and_restore_uses_fenced_fallback(self):
        from mediafiles.models import MediaAsset

        image = MediaAsset.objects.create(
            owner=self.user,
            scope="public",
            category="avatar",
            status="uploaded",
            object_key="public/avatar-fence.jpg",
        )
        content = decide(services.submit("avatar", self.user, candidate=image.pk), self.admin)
        with patch(
            "mediafiles.default_avatars.choose_default_avatar", return_value="public/default.jpg"
        ):
            content = decide(content, self.admin, "withdraw")
        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar_object_key, "public/default.jpg")
        decide(content, self.admin, "restore")
        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar_object_key, image.object_key)
