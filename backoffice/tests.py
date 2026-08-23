from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from mediafiles.models import MediaAsset
from orders.models import ProviderOrder
from providers.models import (
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)

from .models import (
    AdminAuditLog,
    AdminRole,
    Organization,
    OrganizationMember,
    ProviderCreditAdjustment,
    ProviderOrderAfterSalesCase,
    ProviderOrderSupportNote,
    UserRiskFlag,
)


class BackofficeProviderReviewTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_user(
            phone="19900001111", password="test-password", nickname="城市审核员"
        )
        cls.organization = Organization.objects.create(
            name="邯郸运营中心",
            code="handan-operations",
            organization_type=Organization.Type.CITY_AGENT,
            city_codes=["130400"],
        )
        cls.role = AdminRole.objects.create(
            organization=cls.organization,
            name="达人审核员",
            code="provider-reviewer",
            permissions=[
                "dashboard.view",
                "user.view",
                "user.status.manage",
                "user.risk.manage",
                "provider.view",
                "provider.review",
                "provider.manage",
                "provider.credit.adjust",
                "order.fulfillment.view",
                "order.support_note.add",
                "order.after_sales.view",
                "order.after_sales.review",
            ],
            data_scope=AdminRole.DataScope.CITY,
        )
        OrganizationMember.objects.create(
            user=cls.admin_user, organization=cls.organization, role=cls.role
        )
        cls.handan_user = User.objects.create_user(
            phone="19900002222",
            password="test-password",
            nickname="邯郸达人",
            gender=User.Gender.FEMALE,
            verification_status=User.VerificationStatus.VERIFIED,
        )
        cls.beijing_user = User.objects.create_user(
            phone="19900003333",
            password="test-password",
            nickname="北京达人",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        cls.handan_lifestyle_photo = MediaAsset.objects.create(
            owner=cls.handan_user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key="public/provider-photos/handan/lifestyle.webp",
        )
        cls.handan = ProviderProfile.objects.create(
            user=cls.handan_user,
            status=ProviderProfile.Status.PENDING,
            lifestyle_photo=cls.handan_lifestyle_photo,
            service_city_code="130400",
            service_city_name="邯郸市",
            bio="这是用于测试审核流程的达人简介内容。",
        )
        cls.beijing = ProviderProfile.objects.create(
            user=cls.beijing_user,
            status=ProviderProfile.Status.PENDING,
            service_city_code="110100",
            service_city_name="北京市",
            bio="这是另一个城市的达人申请资料内容。",
        )
        cls.order_customer = User.objects.create_user(
            phone="19900008888", password="test-password", nickname="订单用户"
        )
        cls.order_category = ServiceCategory.objects.create(
            name="履约核查服务", slug="fulfillment-admin"
        )

    def setUp(self):
        self.client.force_authenticate(self.admin_user)

    def create_fulfillment_order(
        self, *, order_no, provider, status=ProviderOrder.Status.PENDING_CONFIRMATION,
        with_evidence=True, completion_age_days=0,
    ):
        now = timezone.now()
        service = ProviderService.objects.create(
            provider=provider,
            category=self.order_category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=16800,
        )
        photo = None
        if with_evidence:
            photo = MediaAsset.objects.create(
                owner=provider.user,
                scope=MediaAsset.Scope.PRIVATE,
                category=MediaAsset.Category.ORDER_EVIDENCE,
                status=MediaAsset.Status.UPLOADED,
                object_key=f"private/order-evidence/{order_no}.webp",
            )
        return ProviderOrder.objects.create(
            order_no=order_no,
            customer=self.order_customer,
            provider=provider,
            service=service,
            provider_name_snapshot=provider.user.nickname,
            service_name_snapshot=self.order_category.name,
            billing_type_snapshot=ProviderOrder.BillingType.PER_SESSION,
            unit_price_amount=16800,
            starts_at=now - timedelta(hours=3),
            ends_at=now - timedelta(hours=1),
            duration_minutes=120,
            meeting_address="邯郸市丛台区测试集合地点",
            contact_name="订单用户",
            contact_phone="13812346688",
            service_fee_amount=16800,
            payable_amount=16800,
            status=status,
            payment_expires_at=now - timedelta(days=1),
            paid_at=now - timedelta(days=1),
            accepted_at=now - timedelta(hours=5),
            departed_at=now - timedelta(hours=4),
            arrival_photo=photo,
            arrival_photo_uploaded_at=now - timedelta(hours=3, minutes=30),
            arrival_longitude="114.5060000" if photo else None,
            arrival_latitude="36.6200000" if photo else None,
            arrival_location_accuracy_m="12.50" if photo else None,
            service_started_at=now - timedelta(hours=3),
            completion_submitted_at=now - timedelta(days=completion_age_days),
        )

    def test_city_member_only_sees_scoped_applications(self):
        response = self.client.get(reverse("backoffice-provider-applications"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        items = response.data["data"]["items"]
        self.assertEqual([item["id"] for item in items], [self.handan.id])
        self.assertEqual(items[0]["gender"], User.Gender.FEMALE)
        self.assertIn("lifestyle.webp", items[0]["lifestyle_photo_url"])

    def test_approve_writes_audit_log(self):
        response = self.client.post(
            reverse("backoffice-provider-application-review", args=(self.handan.id,)),
            {"decision": "approve"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.handan.refresh_from_db()
        self.assertEqual(self.handan.status, ProviderProfile.Status.APPROVED)
        audit = AdminAuditLog.objects.get(target_id=str(self.handan.id))
        self.assertEqual(audit.action, "provider.application.approve")
        self.assertEqual(audit.organization, self.organization)

    def test_reject_requires_reason(self):
        response = self.client.post(
            reverse("backoffice-provider-application-review", args=(self.handan.id,)),
            {"decision": "reject", "reason": ""},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_out_of_scope_review_is_rejected(self):
        response = self.client.post(
            reverse("backoffice-provider-application-review", args=(self.beijing.id,)),
            {"decision": "approve"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unverified_application_cannot_be_approved(self):
        user = User.objects.create_user(
            phone="19900005555", password="test-password", nickname="未实名申请人"
        )
        profile = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.PENDING,
            service_city_code="130400",
            service_city_name="邯郸市",
            bio="这是尚未完成实名认证的达人申请资料。",
        )

        response = self.client.post(
            reverse("backoffice-provider-application-review", args=(profile.id,)),
            {"decision": "approve"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        profile.refresh_from_db()
        self.assertEqual(profile.status, ProviderProfile.Status.PENDING)

    def test_application_without_lifestyle_photo_cannot_be_approved(self):
        user = User.objects.create_user(
            phone="19900007777",
            password="test-password",
            nickname="缺少生活照",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        profile = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.PENDING,
            service_city_code="130400",
            service_city_name="邯郸市",
            bio="这是缺少生活照的达人申请资料。",
        )

        response = self.client.post(
            reverse("backoffice-provider-application-review", args=(profile.id,)),
            {"decision": "approve"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("生活照", str(response.data))

    def test_invalid_provider_list_query_returns_validation_error(self):
        response = self.client.get(
            reverse("backoffice-provider-applications"),
            {"status": "unknown", "page": "not-a-number"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_management_list_is_scoped_masked_and_summarized(self):
        response = self.client.get(reverse("backoffice-users"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual([item["nickname"] for item in data["items"]], ["邯郸达人"])
        self.assertEqual(data["items"][0]["phone_masked"], "199****2222")
        self.assertEqual(data["items"][0]["identity"], "provider")
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(data["summary"]["verified"], 1)
        self.assertEqual(data["summary"]["providers"], 1)

    def test_user_suspend_revokes_existing_tokens_and_writes_audit(self):
        auth_version = self.handan_user.auth_version
        response = self.client.post(
            reverse("backoffice-user-account-action", args=(self.handan_user.public_id,)),
            {"action": "suspend", "reason": "多次收到线下服务安全投诉"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.handan_user.refresh_from_db()
        self.assertEqual(self.handan_user.account_status, User.AccountStatus.SUSPENDED)
        self.assertEqual(self.handan_user.auth_version, auth_version + 1)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="user.account.suspend", target_id=str(self.handan_user.public_id)
            ).exists()
        )

    def test_user_risk_flag_can_be_marked_and_cleared_with_audit(self):
        url = reverse("backoffice-user-risk-action", args=(self.handan_user.public_id,))
        marked = self.client.post(
            url,
            {"action": "mark", "level": "high", "reason": "疑似绕过平台进行私下交易"},
            format="json",
        )
        cleared = self.client.post(
            url,
            {"action": "clear", "reason": "复核材料后解除风险标记"},
            format="json",
        )

        self.assertEqual(marked.status_code, status.HTTP_200_OK)
        self.assertEqual(marked.data["data"]["risk_flag"]["level"], "high")
        self.assertEqual(cleared.status_code, status.HTTP_200_OK)
        self.assertIsNone(cleared.data["data"]["risk_flag"])
        flag = UserRiskFlag.objects.get(user=self.handan_user)
        self.assertFalse(flag.is_active)
        self.assertEqual(
            AdminAuditLog.objects.filter(target_id=str(self.handan_user.public_id)).count(), 2
        )

    def test_out_of_scope_user_action_is_rejected(self):
        response = self.client.post(
            reverse("backoffice-user-account-action", args=(self.beijing_user.public_id,)),
            {"action": "suspend", "reason": "跨城市处置测试"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_provider_management_restricts_orders_and_blocks_self_reopen(self):
        self.handan.status = ProviderProfile.Status.APPROVED
        self.handan.is_accepting_orders = True
        self.handan.save(update_fields=("status", "is_accepting_orders", "updated_at"))
        response = self.client.post(
            reverse("backoffice-provider-action", args=(self.handan.id,)),
            {"action": "restrict_orders", "reason": "服务投诉待复核"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.handan.refresh_from_db()
        self.assertTrue(self.handan.admin_order_restricted)
        self.assertFalse(self.handan.is_accepting_orders)
        provider_client = self.client_class()
        provider_client.force_authenticate(self.handan_user)
        reopen = provider_client.patch(
            "/api/v1/providers/me/workbench/",
            {"is_accepting_orders": True},
            format="json",
        )
        self.assertEqual(reopen.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="provider.management.restrict_orders", target_id=str(self.handan.id)
            ).exists()
        )

    def test_provider_qualification_can_be_suspended_and_restored(self):
        self.handan.status = ProviderProfile.Status.APPROVED
        self.handan.save(update_fields=("status", "updated_at"))
        url = reverse("backoffice-provider-action", args=(self.handan.id,))

        suspended = self.client.post(
            url,
            {"action": "suspend_qualification", "reason": "资料真实性复核中"},
            format="json",
        )
        restored = self.client.post(
            url,
            {"action": "restore_qualification", "reason": "复核通过，恢复达人资格"},
            format="json",
        )

        self.assertEqual(suspended.status_code, status.HTTP_200_OK)
        self.assertEqual(suspended.data["data"]["status"], ProviderProfile.Status.SUSPENDED)
        self.assertEqual(restored.status_code, status.HTTP_200_OK)
        self.assertEqual(restored.data["data"]["status"], ProviderProfile.Status.APPROVED)
        self.assertFalse(restored.data["data"]["is_accepting_orders"])

    def test_provider_credit_adjustment_updates_score_and_records_history(self):
        self.handan.status = ProviderProfile.Status.APPROVED
        self.handan.save(update_fields=("status", "updated_at"))
        response = self.client.post(
            reverse("backoffice-provider-credit-adjustment", args=(self.handan.id,)),
            {"delta": -8, "reason": "客服核实达人临时取消服务"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["credit_score"], 92)
        adjustment = ProviderCreditAdjustment.objects.get(provider=self.handan)
        self.assertEqual((adjustment.before_score, adjustment.after_score), (100, 92))
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="provider.credit.adjust", target_id=str(self.handan.id)
            ).exists()
        )

    def test_provider_management_list_is_scoped_and_has_summary(self):
        self.handan.status = ProviderProfile.Status.APPROVED
        self.handan.is_accepting_orders = True
        self.handan.save(update_fields=("status", "is_accepting_orders", "updated_at"))

        response = self.client.get(reverse("backoffice-providers"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual([item["id"] for item in data["items"]], [self.handan.id])
        self.assertEqual(data["items"][0]["phone_masked"], "199****2222")
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(data["summary"]["accepting"], 1)

    def test_user_and_provider_management_permissions_are_required(self):
        restricted_user = User.objects.create_user(
            phone="19900001234", password="test-password", nickname="仅看总览"
        )
        restricted_role = AdminRole.objects.create(
            organization=self.organization,
            name="总览查看员",
            code="people-dashboard-only",
            permissions=["dashboard.view"],
            data_scope=AdminRole.DataScope.CITY,
        )
        OrganizationMember.objects.create(
            user=restricted_user,
            organization=self.organization,
            role=restricted_role,
        )
        self.client.force_authenticate(restricted_user)

        self.assertEqual(
            self.client.get(reverse("backoffice-users")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get(reverse("backoffice-providers")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_invalid_people_management_queries_are_rejected(self):
        user_response = self.client.get(reverse("backoffice-users"), {"risk": "unknown"})
        provider_response = self.client.get(
            reverse("backoffice-providers"), {"accepting": "unknown"}
        )

        self.assertEqual(user_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(provider_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_provider_credit_adjustment_rejects_score_outside_range(self):
        self.handan.status = ProviderProfile.Status.APPROVED
        self.handan.credit_score = 98
        self.handan.save(update_fields=("status", "credit_score", "updated_at"))

        response = self.client.post(
            reverse("backoffice-provider-credit-adjustment", args=(self.handan.id,)),
            {"delta": 3, "reason": "边界值测试"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ProviderCreditAdjustment.objects.filter(provider=self.handan).exists())

    def test_fulfillment_orders_are_city_scoped_and_filter_overdue_confirmation(self):
        handan_order = self.create_fulfillment_order(
            order_no="ADMIN-FULFILLMENT-HANDAN",
            provider=self.handan,
            completion_age_days=4,
        )
        self.create_fulfillment_order(
            order_no="ADMIN-FULFILLMENT-BEIJING",
            provider=self.beijing,
            completion_age_days=4,
        )

        response = self.client.get(
            reverse("backoffice-provider-orders"),
            {"anomaly": "confirmation_overdue"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual([item["order_no"] for item in data["items"]], [handan_order.order_no])
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(data["summary"]["pending_confirmation"], 1)
        self.assertEqual(data["summary"]["anomalies"], 1)
        self.assertTrue(data["items"][0]["arrival_photo_available"])
        self.assertNotIn("arrival_photo_url", data["items"][0])
        self.assertEqual(
            [item["code"] for item in data["items"][0]["anomalies"]],
            ["confirmation_overdue"],
        )
        any_anomaly = self.client.get(
            reverse("backoffice-provider-orders"), {"anomaly": "any"}
        )
        self.assertEqual(
            [item["order_no"] for item in any_anomaly.data["data"]["items"]],
            [handan_order.order_no],
        )

    @patch("backoffice.views.build_media_url", return_value="https://media.test/private.webp")
    def test_evidence_access_is_signed_and_audited(self, _build_media_url):
        order = self.create_fulfillment_order(
            order_no="ADMIN-EVIDENCE-AUDIT",
            provider=self.handan,
        )

        response = self.client.get(
            reverse("backoffice-provider-order-evidence", args=(order.order_no,))
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["url"], "https://media.test/private.webp")
        audit = AdminAuditLog.objects.get(
            action="order.fulfillment.evidence.view", target_id=order.order_no
        )
        self.assertEqual(audit.organization, self.organization)

    def test_support_note_is_append_only_and_audited(self):
        order = self.create_fulfillment_order(
            order_no="ADMIN-SUPPORT-NOTE",
            provider=self.handan,
        )

        response = self.client.post(
            reverse("backoffice-provider-order-support-note", args=(order.order_no,)),
            {"content": "已电话联系用户，等待用户确认服务结果。"},
            format="json",
        )
        detail = self.client.get(
            reverse("backoffice-provider-order-detail", args=(order.order_no,))
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ProviderOrderSupportNote.objects.filter(order=order).count(), 1)
        self.assertEqual(detail.data["data"]["support_notes"][0]["content"], response.data["data"]["content"])
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="order.support_note.add", target_id=order.order_no
            ).exists()
        )

    def test_after_sales_case_create_review_and_approve_are_audited(self):
        order = self.create_fulfillment_order(
            order_no="ADMIN-AFTER-SALES-APPROVE",
            provider=self.handan,
        )
        create = self.client.post(
            reverse("backoffice-provider-order-after-sales"),
            {
                "order_no": order.order_no,
                "case_type": ProviderOrderAfterSalesCase.CaseType.REFUND,
                "requested_amount": 16800,
                "reason": "用户反馈服务未按约定完成，申请退款。",
            },
            format="json",
        )

        self.assertEqual(create.status_code, status.HTTP_201_CREATED)
        case_no = create.data["data"]["case_no"]
        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.AFTER_SALES)
        self.assertEqual(create.data["data"]["order_payable_amount"], 16800)

        start = self.client.post(
            reverse("backoffice-provider-order-after-sales-action", args=(case_no,)),
            {"action": "start_review"},
            format="json",
        )
        approve = self.client.post(
            reverse("backoffice-provider-order-after-sales-action", args=(case_no,)),
            {
                "action": "approve",
                "approved_amount": 12800,
                "result_note": "核查履约记录后，同意部分退款。",
            },
            format="json",
        )

        self.assertEqual(start.status_code, status.HTTP_200_OK)
        self.assertEqual(start.data["data"]["status"], ProviderOrderAfterSalesCase.Status.PROCESSING)
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        self.assertEqual(approve.data["data"]["status"], ProviderOrderAfterSalesCase.Status.APPROVED)
        self.assertEqual(approve.data["data"]["approved_amount"], 12800)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="order.after_sales.approve", target_id=case_no
            ).exists()
        )

    def test_after_sales_reject_restores_original_order_status(self):
        order = self.create_fulfillment_order(
            order_no="ADMIN-AFTER-SALES-REJECT",
            provider=self.handan,
            status=ProviderOrder.Status.PENDING_REVIEW,
        )
        create = self.client.post(
            reverse("backoffice-provider-order-after-sales"),
            {
                "order_no": order.order_no,
                "case_type": ProviderOrderAfterSalesCase.CaseType.SERVICE_DISPUTE,
                "requested_amount": 0,
                "reason": "用户对服务时长存在争议，申请平台复核。",
            },
            format="json",
        )
        case_no = create.data["data"]["case_no"]

        reject = self.client.post(
            reverse("backoffice-provider-order-after-sales-action", args=(case_no,)),
            {"action": "reject", "result_note": "履约时间线与照片完整，售后申请不成立。"},
            format="json",
        )

        self.assertEqual(reject.status_code, status.HTTP_200_OK)
        self.assertEqual(reject.data["data"]["status"], ProviderOrderAfterSalesCase.Status.REJECTED)
        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_REVIEW)

    def test_after_sales_rejects_duplicate_open_case_and_excess_amount(self):
        order = self.create_fulfillment_order(
            order_no="ADMIN-AFTER-SALES-VALIDATION", provider=self.handan
        )
        payload = {
            "order_no": order.order_no,
            "case_type": ProviderOrderAfterSalesCase.CaseType.REFUND,
            "requested_amount": 16800,
            "reason": "用户申请平台核查并处理退款。",
        }
        first = self.client.post(
            reverse("backoffice-provider-order-after-sales"), payload, format="json"
        )
        duplicate = self.client.post(
            reverse("backoffice-provider-order-after-sales"), payload, format="json"
        )
        excess = self.client.post(
            reverse("backoffice-provider-order-after-sales-action", args=(first.data["data"]["case_no"],)),
            {
                "action": "approve",
                "approved_amount": 16801,
                "result_note": "测试核准金额超过申请金额。",
            },
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(excess.status_code, status.HTTP_400_BAD_REQUEST)

    def test_after_sales_list_is_city_scoped(self):
        handan_order = self.create_fulfillment_order(
            order_no="ADMIN-AFTER-SALES-HANDAN", provider=self.handan
        )
        beijing_order = self.create_fulfillment_order(
            order_no="ADMIN-AFTER-SALES-BEIJING", provider=self.beijing
        )
        ProviderOrderAfterSalesCase.objects.create(
            order=handan_order,
            creator=self.admin_user,
            organization=self.organization,
            case_type=ProviderOrderAfterSalesCase.CaseType.OTHER,
            original_order_status=handan_order.status,
            reason="邯郸订单售后核查。",
        )
        ProviderOrderAfterSalesCase.objects.create(
            order=beijing_order,
            creator=self.admin_user,
            organization=self.organization,
            case_type=ProviderOrderAfterSalesCase.CaseType.OTHER,
            original_order_status=beijing_order.status,
            reason="北京订单售后核查。",
        )

        response = self.client.get(reverse("backoffice-provider-order-after-sales"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["summary"]["total"], 1)
        self.assertEqual(
            [item["order_no"] for item in response.data["data"]["items"]],
            [handan_order.order_no],
        )

    def test_order_permission_is_required(self):
        restricted_user = User.objects.create_user(
            phone="19900009999", password="test-password", nickname="仅看总览"
        )
        restricted_role = AdminRole.objects.create(
            organization=self.organization,
            name="总览查看员",
            code="dashboard-only",
            permissions=["dashboard.view"],
            data_scope=AdminRole.DataScope.CITY,
        )
        OrganizationMember.objects.create(
            user=restricted_user,
            organization=self.organization,
            role=restricted_role,
        )
        self.client.force_authenticate(restricted_user)

        response = self.client.get(reverse("backoffice-provider-orders"))
        after_sales = self.client.get(reverse("backoffice-provider-order-after-sales"))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(after_sales.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_order_anomaly_filter_returns_validation_error(self):
        response = self.client.get(
            reverse("backoffice-provider-orders"), {"anomaly": "unknown"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_overview_returns_scoped_order_and_transaction_trends(self):
        customer = User.objects.create_user(
            phone="19900006666", password="test-password", nickname="趋势测试用户"
        )
        category = ServiceCategory.objects.create(name="趋势服务", slug="overview-trend")
        handan_service = ProviderService.objects.create(
            provider=self.handan,
            category=category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=1000,
        )
        beijing_service = ProviderService.objects.create(
            provider=self.beijing,
            category=category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=9000,
        )

        def create_order(*, order_no, provider, service, days_ago, amount, paid):
            date = timezone.localdate() - timedelta(days=days_ago)
            occurred_at = timezone.make_aware(
                datetime.combine(date, time(12)), timezone.get_current_timezone()
            )
            if days_ago == 0:
                occurred_at = timezone.now() - timedelta(minutes=1)
            starts_at = timezone.now() + timedelta(days=1)
            order = ProviderOrder.objects.create(
                order_no=order_no,
                customer=customer,
                provider=provider,
                service=service,
                provider_name_snapshot=provider.user.nickname,
                service_name_snapshot=category.name,
                billing_type_snapshot=ProviderOrder.BillingType.PER_SESSION,
                unit_price_amount=amount,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=2),
                duration_minutes=120,
                meeting_address="测试集合地点",
                contact_name="趋势用户",
                contact_phone="13800000000",
                service_fee_amount=amount,
                payable_amount=amount,
                status=(
                    ProviderOrder.Status.PENDING_ACCEPTANCE
                    if paid else ProviderOrder.Status.PENDING_PAYMENT
                ),
                payment_expires_at=timezone.now() + timedelta(minutes=15),
            )
            ProviderOrder.objects.filter(id=order.id).update(
                created_at=occurred_at,
                updated_at=occurred_at,
                paid_at=occurred_at if paid else None,
            )

        create_order(
            order_no="TREND-HANDAN-TODAY",
            provider=self.handan,
            service=handan_service,
            days_ago=0,
            amount=1000,
            paid=True,
        )
        create_order(
            order_no="TREND-HANDAN-YESTERDAY-PAID",
            provider=self.handan,
            service=handan_service,
            days_ago=1,
            amount=2500,
            paid=True,
        )
        create_order(
            order_no="TREND-HANDAN-YESTERDAY-UNPAID",
            provider=self.handan,
            service=handan_service,
            days_ago=1,
            amount=5000,
            paid=False,
        )
        create_order(
            order_no="TREND-HANDAN-OLDER",
            provider=self.handan,
            service=handan_service,
            days_ago=8,
            amount=4000,
            paid=True,
        )
        create_order(
            order_no="TREND-BEIJING-EXCLUDED",
            provider=self.beijing,
            service=beijing_service,
            days_ago=0,
            amount=9000,
            paid=True,
        )

        response = self.client.get(reverse("backoffice-overview"), {"days": 7})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["trend"]["days"], 7)
        self.assertEqual(len(data["trend"]["points"]), 7)
        points = {point["date"]: point for point in data["trend"]["points"]}
        today = timezone.localdate().isoformat()
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertEqual(points[today]["transaction_amount"], 1000)
        self.assertEqual(points[today]["order_count"], 1)
        self.assertEqual(points[yesterday]["transaction_amount"], 2500)
        self.assertEqual(points[yesterday]["order_count"], 2)
        self.assertEqual(data["metrics"]["week_transaction_amount"], 3500)

        response = self.client.get(reverse("backoffice-overview"), {"days": 30})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]["trend"]["points"]), 30)
        older = (timezone.localdate() - timedelta(days=8)).isoformat()
        points = {point["date"]: point for point in response.data["data"]["trend"]["points"]}
        self.assertEqual(points[older]["transaction_amount"], 4000)

    def test_overview_rejects_unsupported_trend_range(self):
        response = self.client.get(reverse("backoffice-overview"), {"days": 14})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_provider_lifecycle_reaches_public_bookable_availability(self):
        response = self.client.post(
            reverse("backoffice-provider-application-review", args=(self.handan.id,)),
            {"decision": "approve"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.handan.refresh_from_db()
        self.assertFalse(self.handan.is_accepting_orders)

        category = ServiceCategory.objects.create(name="城市陪伴", slug="lifecycle-service")
        provider_client = self.client_class()
        provider_client.force_authenticate(self.handan_user)
        service_response = provider_client.post(
            "/api/v1/providers/me/services/",
            {
                "category_id": category.id,
                "billing_type": ProviderService.BillingType.HOURLY,
                "price_amount": 16800,
                "estimated_duration_minutes": 120,
                "description": "邯郸城市陪伴服务",
            },
            format="json",
        )
        self.assertEqual(service_response.status_code, status.HTTP_201_CREATED)
        service_id = service_response.data["data"]["id"]
        day = timezone.localdate() + timedelta(days=1)
        ProviderWeeklyAvailability.objects.create(
            provider=self.handan,
            weekday=day.weekday(),
            starts_at=time(13),
            ends_at=time(17),
        )
        provider_client.patch(
            "/api/v1/providers/me/workbench/",
            {"is_accepting_orders": True},
            format="json",
        )

        public_client = self.client_class()
        detail = public_client.get(f"/api/v1/providers/{self.handan_user.public_id}/")
        availability = public_client.get(
            f"/api/v1/providers/{self.handan_user.public_id}/availability/",
            {"service_id": service_id, "start_date": day.isoformat(), "days": 1},
        )

        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["data"]["services"][0]["id"], service_id)
        self.assertTrue(availability.data["data"]["dates"][0]["slots"])

    def test_user_without_membership_is_forbidden(self):
        outsider = User.objects.create_user(
            phone="19900004444", password="test-password", nickname="普通用户"
        )
        self.client.force_authenticate(outsider)
        response = self.client.get(reverse("backoffice-provider-applications"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
