import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from activities.models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityCategory,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivityRefundRecord,
    ActivityReport,
    ActivitySettlement,
)
from activities.services import process_activity_timeouts
from locations.models import UserAddress
from mediafiles.models import MediaAsset
from notifications.models import UserNotification
from orders.models import (
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderRefundOrder,
    ProviderOrderReview,
)
from orders.services import create_provider_order_payment_order
from providers.models import (
    ProviderLiveLocation,
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)
from taskcenter.models import ScheduledTask
from taskcenter.services import register_provider_order_confirmation_timeout

from .models import (
    AdminAuditLog,
    AdminRole,
    Organization,
    OrganizationMember,
    ProviderCreditAdjustment,
    ProviderOrderAfterSalesCase,
    ProviderOrderSupportNote,
    ProviderOrderingSetting,
    PlatformOperationSetting,
    UserRiskFlag,
)


class BackofficeProviderReviewTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_user(
            phone="19900001111", password="test-password", nickname="城市审核员"
        )
        cls.platform_admin = User.objects.create_superuser(
            phone="19900001112", password="test-password", nickname="平台超级管理员"
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
                "service_category.view",
                "service_category.manage",
                "order.fulfillment.view",
                "order.support_note.add",
                "order.review.manage",
                "order.after_sales.view",
                "order.after_sales.review",
                "order.finance.view",
                "order.finance.manage",
                "audit.view",
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

    def create_live_location(self, provider, *, accuracy_m="16.00"):
        now = timezone.now()
        return ProviderLiveLocation.objects.create(
            provider=provider,
            session_id=uuid.uuid4(),
            source_longitude=Decimal("114.5389610"),
            source_latitude=Decimal("36.6256570"),
            position=Point(114.5328, 36.6252, srid=4326),
            accuracy_m=Decimal(accuracy_m),
            located_at=now,
            received_at=now,
        )

    def create_fulfillment_order(
        self, *, order_no, provider, status=ProviderOrder.Status.PENDING_CONFIRMATION,
        with_evidence=True, completion_age_days=0,
    ):
        now = timezone.now()
        completion_submitted_at = now - timedelta(days=completion_age_days)
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
        order = ProviderOrder.objects.create(
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
            completion_submitted_at=completion_submitted_at,
            confirmation_expires_at=completion_submitted_at + timedelta(days=3),
        )
        payment, _ = create_provider_order_payment_order(order)
        payment.status = ProviderOrderPaymentOrder.Status.PAID
        payment.channel = ProviderOrderPaymentOrder.Channel.MOCK_WECHAT
        payment.gateway_trade_no = f"MOCK-{order_no}"
        payment.paid_at = order.paid_at
        payment.save(
            update_fields=(
                "status", "channel", "gateway_trade_no", "paid_at", "updated_at",
            )
        )
        return order

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
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.handan_user,
                event_type=UserNotification.EventType.PROVIDER_APPLICATION_RESULT,
            ).exists()
        )

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

    def test_user_detail_includes_address_contact_and_coordinates(self):
        UserAddress.objects.create(
            user=self.handan_user,
            name="邯郸美乐城",
            address="人民东路456号",
            city_name="邯郸市",
            contact_name="王女士",
            contact_gender=UserAddress.ContactGender.MS,
            contact_phone="18800006666",
            longitude="114.5120000",
            latitude="36.6130000",
            is_default=True,
        )

        response = self.client.get(
            reverse("backoffice-user-detail", args=(self.handan_user.public_id,))
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        address = response.data["data"]["addresses"][0]
        self.assertEqual(address["contact_gender_label"], "女士")
        self.assertEqual(address["contact_phone"], "18800006666")
        self.assertEqual(Decimal(address["longitude"]), Decimal("114.5120000"))

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
        reopen = provider_client.post(
            "/api/v1/providers/me/online/start/",
            {"longitude": "114.5389610", "latitude": "36.6256570", "accuracy_m": "16"},
            format="json",
        )
        self.assertEqual(reopen.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="provider.management.restrict_orders", target_id=str(self.handan.id)
            ).exists()
        )
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.handan_user,
                event_type=UserNotification.EventType.PROVIDER_STATUS_CHANGED,
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
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.handan_user,
                event_type=UserNotification.EventType.PROVIDER_CREDIT_CHANGED,
            ).exists()
        )

    def test_provider_management_list_is_scoped_and_has_summary(self):
        self.handan.status = ProviderProfile.Status.APPROVED
        self.handan.is_accepting_orders = True
        self.handan.save(update_fields=("status", "is_accepting_orders", "updated_at"))
        self.create_live_location(self.handan)

        response = self.client.get(reverse("backoffice-providers"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual([item["id"] for item in data["items"]], [self.handan.id])
        self.assertEqual(data["items"][0]["phone_masked"], "199****2222")
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(data["summary"]["accepting"], 1)

    def test_provider_management_detail_includes_current_live_location(self):
        self.handan.status = ProviderProfile.Status.APPROVED
        self.handan.is_accepting_orders = True
        self.handan.save(update_fields=("status", "is_accepting_orders", "updated_at"))
        self.create_live_location(self.handan)

        response = self.client.get(
            reverse("backoffice-provider-detail", args=(self.handan.id,))
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertTrue(data["is_online"])
        self.assertTrue(data["has_live_location"])
        self.assertEqual(data["current_longitude"], "114.5389610")
        self.assertEqual(data["location_accuracy_m"], "16.00")
        self.assertIsNotNone(data["location_updated_at"])

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
        self.assertEqual(
            self.client.get(reverse("backoffice-service-categories")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get(reverse("backoffice-provider-ordering-setting")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get(reverse("backoffice-platform-operation-setting")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_city_scoped_role_cannot_access_global_operation_settings(self):
        self.role.permissions = [*self.role.permissions, "operations.manage"]
        self.role.save(update_fields=("permissions", "updated_at"))

        self.assertEqual(
            self.client.get(reverse("backoffice-provider-ordering-setting")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.patch(
                reverse("backoffice-platform-operation-setting"),
                {"provider_order_payment_timeout_minutes": 20},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_service_category_list_has_summary_and_relationship_counts(self):
        ProviderService.objects.create(
            provider=self.handan,
            category=self.order_category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=16800,
        )
        ServiceCategory.objects.create(
            name="已停用分类",
            slug="inactive-admin-category",
            is_active=False,
            sort_order=99,
        )

        response = self.client.get(reverse("backoffice-service-categories"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["active"], 1)
        self.assertEqual(data["summary"]["inactive"], 1)
        self.assertEqual(data["summary"]["active_services"], 1)
        category = next(
            item for item in data["items"] if item["id"] == self.order_category.id
        )
        self.assertEqual(category["service_count"], 1)
        self.assertEqual(category["provider_count"], 1)

    def test_service_category_create_and_disable_are_audited(self):
        create_response = self.client.post(
            reverse("backoffice-service-categories"),
            {
                "name": "桌游陪玩",
                "slug": "board-games-admin",
                "city_codes": ["130400", "130400", "110100"],
                "sort_order": 8,
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        category_id = create_response.data["data"]["id"]
        self.assertEqual(
            create_response.data["data"]["city_codes"], ["130400", "110100"]
        )

        update_response = self.client.patch(
            reverse("backoffice-service-category-detail", args=(category_id,)),
            {"is_active": False, "sort_order": 18},
            format="json",
        )

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertFalse(update_response.data["data"]["is_active"])
        self.assertEqual(update_response.data["data"]["sort_order"], 18)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="service_category.create",
                target_id=str(category_id),
            ).exists()
        )
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="service_category.disable",
                target_id=str(category_id),
            ).exists()
        )

    def test_provider_ordering_setting_can_be_updated_and_is_audited(self):
        self.client.force_authenticate(self.platform_admin)
        detail_url = reverse("backoffice-provider-ordering-setting")

        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["location_report_interval_seconds"], 300)

        response = self.client.patch(
            detail_url,
            {
                "location_report_interval_seconds": 180,
                "location_timeout_minutes": 45,
                "max_location_accuracy_m": 150,
                "acceptance_timeout_minutes": 20,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        setting = ProviderOrderingSetting.current()
        self.assertEqual(setting.location_report_interval_seconds, 180)
        self.assertEqual(setting.location_timeout_minutes, 45)
        self.assertEqual(setting.max_location_accuracy_m, 150)
        self.assertEqual(setting.acceptance_timeout_minutes, 20)
        audit = AdminAuditLog.objects.get(
            action="operations.provider_ordering.update",
            target_id="provider-ordering",
        )
        self.assertEqual(audit.before["location_timeout_minutes"], 30)
        self.assertEqual(audit.after["location_timeout_minutes"], 45)

        invalid_response = self.client.patch(
            detail_url,
            {"max_location_accuracy_m": 201},
            format="json",
        )
        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ProviderOrderingSetting.current().max_location_accuracy_m, 150)

    def test_platform_operation_setting_can_be_updated_and_is_audited(self):
        self.client.force_authenticate(self.platform_admin)
        detail_url = reverse("backoffice-platform-operation-setting")

        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["data"]["provider_order_payment_timeout_minutes"], 15
        )
        self.assertEqual(
            response.data["data"]["provider_order_settlement_freeze_days"], 1
        )

        response = self.client.patch(
            detail_url,
            {
                "provider_order_payment_timeout_minutes": 20,
                "provider_order_confirmation_timeout_days": 5,
                "provider_order_settlement_freeze_days": 2,
                "activity_payment_timeout_minutes": 15,
                "activity_minimum_advance_hours": 24,
                "activity_maximum_advance_days": 45,
                "activity_settlement_confirmation_hours": 36,
                "activity_settlement_risk_freeze_days": 10,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        setting = PlatformOperationSetting.current()
        self.assertEqual(setting.provider_order_payment_timeout_minutes, 20)
        self.assertEqual(setting.provider_order_confirmation_timeout_days, 5)
        self.assertEqual(setting.provider_order_settlement_freeze_days, 2)
        self.assertEqual(setting.activity_payment_timeout_minutes, 15)
        self.assertEqual(setting.activity_minimum_advance_hours, 24)
        self.assertEqual(setting.activity_maximum_advance_days, 45)
        self.assertEqual(setting.activity_settlement_confirmation_hours, 36)
        self.assertEqual(setting.activity_settlement_risk_freeze_days, 10)
        audit = AdminAuditLog.objects.get(
            action="operations.platform.update",
            target_id="platform",
        )
        self.assertEqual(audit.before["activity_payment_timeout_minutes"], 30)
        self.assertEqual(audit.after["activity_payment_timeout_minutes"], 15)

        invalid_response = self.client.patch(
            detail_url,
            {"provider_order_payment_timeout_minutes": 61},
            format="json",
        )
        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            PlatformOperationSetting.current().provider_order_payment_timeout_minutes,
            20,
        )

    def test_linked_service_category_slug_cannot_change(self):
        ProviderService.objects.create(
            provider=self.handan,
            category=self.order_category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=16800,
        )

        response = self.client.patch(
            reverse(
                "backoffice-service-category-detail",
                args=(self.order_category.id,),
            ),
            {"slug": "changed-linked-slug"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order_category.refresh_from_db()
        self.assertEqual(self.order_category.slug, "fulfillment-admin")

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

    def test_order_review_can_be_hidden_and_restored_with_audit(self):
        order = self.create_fulfillment_order(
            order_no="ADMIN-REVIEW-MODERATE",
            provider=self.handan,
            status=ProviderOrder.Status.COMPLETED,
        )
        review = ProviderOrderReview.objects.create(
            order=order,
            customer=self.order_customer,
            provider=self.handan,
            rating=5,
            content="服务很周到",
        )
        self.handan.rating = Decimal("5.00")
        self.handan.service_count = 1
        self.handan.save(update_fields=("rating", "service_count", "updated_at"))
        url = reverse("backoffice-provider-order-review-action", args=(order.order_no,))

        hidden = self.client.post(
            url, {"action": "hide", "reason": "内容包含用户隐私"}, format="json"
        )

        self.assertEqual(hidden.status_code, status.HTTP_200_OK)
        self.assertFalse(hidden.data["data"]["review"]["is_visible"])
        review.refresh_from_db()
        self.handan.refresh_from_db()
        self.assertFalse(review.is_visible)
        self.assertEqual(self.handan.rating, Decimal("0.00"))
        self.assertEqual(self.handan.service_count, 1)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="order.review.hide", target_id=str(review.id)
            ).exists()
        )

        restored = self.client.post(url, {"action": "restore"}, format="json")

        self.assertEqual(restored.status_code, status.HTTP_200_OK)
        self.assertTrue(restored.data["data"]["review"]["is_visible"])
        self.handan.refresh_from_db()
        self.assertEqual(self.handan.rating, Decimal("5.00"))
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="order.review.restore", target_id=str(review.id)
            ).exists()
        )

    def test_audit_logs_support_search_filters_and_pagination(self):
        AdminAuditLog.objects.create(
            actor=self.admin_user, organization=self.organization,
            action="order.support_note.add", target_type="order", target_id="AUDIT-ORDER-1",
            before={}, after={"content": "已联系"},
        )
        AdminAuditLog.objects.create(
            actor=self.admin_user, organization=self.organization,
            action="provider.application.approve", target_type="provider", target_id="AUDIT-PROVIDER-1",
            before={"status": "pending"}, after={"status": "approved"},
        )
        response = self.client.get(reverse("backoffice-audit-logs"), {
            "search": "AUDIT-ORDER", "target_type": "order", "page": 1, "page_size": 1,
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["pagination"], {"page": 1, "page_size": 1, "total": 1})
        self.assertEqual(data["items"][0]["target_id"], "AUDIT-ORDER-1")
        invalid = self.client.get(
            reverse("backoffice-audit-logs"), {"page": "invalid"}
        )
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

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
        self.assertEqual(approve.data["data"]["status"], ProviderOrderAfterSalesCase.Status.REFUNDED)
        self.assertEqual(approve.data["data"]["approved_amount"], 12800)
        refund = ProviderOrderRefundOrder.objects.get(
            idempotency_key=f"provider-after-sales:{case_no}"
        )
        self.assertEqual(refund.status, ProviderOrderRefundOrder.Status.SUCCEEDED)
        self.assertEqual(refund.refund_amount, 12800)
        self.assertEqual(refund.service_fee_refund_amount, 12800)
        self.assertEqual(refund.transport_fee_refund_amount, 0)
        self.assertTrue(refund.gateway_refund_no.startswith("MOCKREF"))
        self.assertEqual(
            approve.data["data"]["refund_order"]["refund_no"], refund.refund_no
        )
        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_CONFIRMATION)
        order.payment_order.refresh_from_db()
        self.assertEqual(
            order.payment_order.status,
            ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
        )
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="order.after_sales.approve", target_id=case_no
            ).exists()
        )
        self.assertSetEqual(
            set(
                UserNotification.objects.filter(
                    recipient=order.customer,
                    target_id=order.order_no,
                ).values_list("event_type", flat=True)
            ),
            {
                UserNotification.EventType.ORDER_AFTER_SALES_STARTED,
                UserNotification.EventType.ORDER_AFTER_SALES_RESULT,
                UserNotification.EventType.ORDER_REFUND_COMPLETED,
            },
        )

        finance = self.client.get(
            reverse("backoffice-provider-order-finance"),
            {"record_type": "refund", "search": refund.refund_no},
        )
        self.assertEqual(finance.status_code, status.HTTP_200_OK)
        self.assertEqual(finance.data["data"]["pagination"]["total"], 1)
        self.assertEqual(finance.data["data"]["items"][0]["refund_no"], refund.refund_no)
        self.assertEqual(finance.data["data"]["summary"]["refunded_amount"], 12800)

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

    def test_failed_full_refund_can_be_retried_from_finance_center(self):
        order = self.create_fulfillment_order(
            order_no="ADMIN-AFTER-SALES-RETRY",
            provider=self.handan,
        )
        created = self.client.post(
            reverse("backoffice-provider-order-after-sales"),
            {
                "order_no": order.order_no,
                "case_type": ProviderOrderAfterSalesCase.CaseType.REFUND,
                "requested_amount": 16800,
                "reason": "测试退款渠道失败后的重试闭环。",
            },
            format="json",
        )
        case_no = created.data["data"]["case_no"]

        with patch(
            "orders.payment_gateway.MockProviderOrderPaymentGateway.refund",
            side_effect=RuntimeError("模拟支付渠道超时"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse(
                        "backoffice-provider-order-after-sales-action",
                        args=(case_no,),
                    ),
                    {
                        "action": "approve",
                        "approved_amount": 16800,
                        "result_note": "核查后同意订单全额退款。",
                    },
                    format="json",
                )

        case = ProviderOrderAfterSalesCase.objects.get(case_no=case_no)
        refund = ProviderOrderRefundOrder.objects.get(
            idempotency_key=f"provider-after-sales:{case_no}"
        )
        self.assertEqual(case.status, ProviderOrderAfterSalesCase.Status.APPROVED)
        self.assertEqual(refund.status, ProviderOrderRefundOrder.Status.FAILED)
        self.assertIn("支付渠道超时", refund.failure_reason)

        retried = self.client.post(
            reverse("backoffice-provider-order-refund-retry", args=(refund.refund_no,)),
            {},
            format="json",
        )

        self.assertEqual(retried.status_code, status.HTTP_200_OK)
        self.assertEqual(
            retried.data["data"]["status"], ProviderOrderRefundOrder.Status.SUCCEEDED
        )
        case.refresh_from_db()
        order.refresh_from_db()
        order.payment_order.refresh_from_db()
        self.assertEqual(case.status, ProviderOrderAfterSalesCase.Status.REFUNDED)
        self.assertEqual(order.status, ProviderOrder.Status.REFUNDED)
        self.assertEqual(
            order.payment_order.status, ProviderOrderPaymentOrder.Status.REFUNDED
        )
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="order.finance.refund.retry", target_id=refund.refund_no
            ).exists()
        )

    def test_after_sales_pauses_and_reopens_confirmation_timeout(self):
        order = self.create_fulfillment_order(
            order_no="ADMIN-AFTER-SALES-CONFIRM",
            provider=self.handan,
        )
        task, _ = register_provider_order_confirmation_timeout(order)

        create = self.client.post(
            reverse("backoffice-provider-order-after-sales"),
            {
                "order_no": order.order_no,
                "case_type": ProviderOrderAfterSalesCase.CaseType.SERVICE_DISPUTE,
                "requested_amount": 0,
                "reason": "确认期内用户提出服务争议，申请平台复核。",
            },
            format="json",
        )

        self.assertEqual(create.status_code, status.HTTP_201_CREATED)
        task.refresh_from_db()
        self.assertEqual(task.status, ScheduledTask.Status.CANCELLED)

        reject = self.client.post(
            reverse(
                "backoffice-provider-order-after-sales-action",
                args=(create.data["data"]["case_no"],),
            ),
            {"action": "reject", "result_note": "履约记录完整，售后申请不成立。"},
            format="json",
        )

        self.assertEqual(reject.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_CONFIRMATION)
        self.assertEqual(task.status, ScheduledTask.Status.PENDING)
        self.assertEqual(task.available_at, order.confirmation_expires_at)

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
        accepting_response = provider_client.post(
            "/api/v1/providers/me/online/start/",
            {
                "longitude": "114.5389610",
                "latitude": "36.6256570",
                "accuracy_m": "16.00",
            },
            format="json",
        )
        self.assertEqual(accepting_response.status_code, status.HTTP_200_OK)

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


class BackofficeActivityManagementTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_user(
            phone="18890001111", password="test-password", nickname="活动审核员"
        )
        cls.organization = Organization.objects.create(
            name="邯郸活动运营中心",
            code="handan-activity-operations",
            organization_type=Organization.Type.CITY_AGENT,
            city_codes=["130400"],
        )
        cls.role = AdminRole.objects.create(
            organization=cls.organization,
            name="活动审核角色",
            code="activity-reviewer",
            permissions=[
                "activity.view", "activity.review", "activity.manage",
                "activity_category.view", "activity_category.manage",
                "activity_report.view", "activity_report.manage",
                "activity_finance.view", "activity_after_sales.manage",
                "activity_settlement.manage",
            ],
            data_scope=AdminRole.DataScope.CITY,
        )
        OrganizationMember.objects.create(
            user=cls.admin_user, organization=cls.organization, role=cls.role
        )
        cls.organizer = User.objects.create_user(
            phone="18890002222",
            password="test-password",
            nickname="邯郸发起人",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        cls.beijing_organizer = User.objects.create_user(
            phone="18890003333",
            password="test-password",
            nickname="北京发起人",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        cls.participant = User.objects.create_user(
            phone="18890004444", password="test-password", nickname="报名用户"
        )
        cls.category = ActivityCategory.objects.create(name="桌球", slug="admin-billiards")
        cls.activity_cover = MediaAsset.objects.create(
            owner=cls.organizer,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.ACTIVITY_COVER,
            status=MediaAsset.Status.UPLOADED,
            object_key="public/activity-covers/admin/activity.webp",
        )
        cls.handan_activity = cls.create_activity(
            organizer=cls.organizer,
            title="邯郸周末桌球局",
            city_code="130400",
            city_name="邯郸市",
        )
        cls.beijing_activity = cls.create_activity(
            organizer=cls.beijing_organizer,
            title="北京周末桌球局",
            city_code="110100",
            city_name="北京市",
        )
        participation = ActivityParticipation.objects.create(
            activity=cls.handan_activity,
            user=cls.participant,
            status=ActivityParticipation.Status.ACTIVE,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            refund_rule_snapshot={"version": "standard-v1"},
            rule_confirmed_at=timezone.now(),
            joined_at=timezone.now(),
        )
        ActivityParticipationPaymentOrder.objects.create(
            participation=participation,
            payer=cls.participant,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            channel=ActivityParticipationPaymentOrder.Channel.MOCK_WECHAT,
            status=ActivityParticipationPaymentOrder.Status.PAID,
            expires_at=timezone.now() + timedelta(minutes=30),
            paid_at=timezone.now(),
        )

    @classmethod
    def create_activity(cls, *, organizer, title, city_code, city_name):
        starts_at = timezone.now() + timedelta(days=5)
        activity = Activity.objects.create(
            organizer=organizer,
            category=cls.category,
            cover=cls.activity_cover,
            title=title,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=3),
            formation_deadline=starts_at - timedelta(days=1),
            meeting_place_name="测试桌球俱乐部",
            meeting_address="人民东路 128 号",
            city_code=city_code,
            city_name=city_name,
            source_longitude=Decimal("114.4921000"),
            source_latitude=Decimal("36.6123000"),
            meeting_point=Point(114.4859, 36.6118, srid=4326),
            capacity=8,
            min_participants=4,
            description="用于测试后台活动审核的活动介绍。",
            participation_rules="守时参加，文明交流。",
            aa_principal_amount=4800,
            refund_template_version="standard-v1",
            refund_rule_snapshot={"before_24h": "full"},
            status=Activity.Status.PENDING_REVIEW,
        )
        ActivityPublishOrder.objects.create(
            order_no=f"ADMIN-ACTIVITY-{activity.id}",
            activity=activity,
            payer=organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityPublishOrder.Status.PAID,
            paid_at=timezone.now(),
        )
        return activity

    def setUp(self):
        self.client.force_authenticate(self.admin_user)

    def test_activity_list_is_city_scoped_and_has_financial_summary(self):
        response = self.client.get(reverse("backoffice-activities"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual([item["id"] for item in data["items"]], [self.handan_activity.id])
        self.assertEqual(data["summary"]["pending_review"], 1)
        self.assertEqual(data["items"][0]["organizer_phone_masked"], "188****2222")
        self.assertEqual(data["items"][0]["publish_order"]["payable_amount"], 5280)
        self.assertEqual(data["items"][0]["participant_count"], 1)

    def test_activity_detail_includes_participants_and_coordinates(self):
        response = self.client.get(
            reverse("backoffice-activity-detail", args=(self.handan_activity.id,))
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["participants"][0]["phone_masked"], "188****4444")
        self.assertEqual(data["source_longitude"], "114.4921000")

    def test_activity_approve_is_atomic_and_audited(self):
        response = self.client.post(
            reverse("backoffice-activity-review", args=(self.handan_activity.id,)),
            {"decision": "approve"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.handan_activity.refresh_from_db()
        self.assertEqual(self.handan_activity.status, Activity.Status.RECRUITING)
        self.assertEqual(self.handan_activity.reviewed_by, self.admin_user)
        self.assertIsNotNone(self.handan_activity.published_at)
        self.assertTrue(AdminAuditLog.objects.filter(
            action="activity.review.approve", target_id=str(self.handan_activity.id)
        ).exists())
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.organizer,
                event_type=UserNotification.EventType.ACTIVITY_REVIEW_RESULT,
                target_id=str(self.handan_activity.id),
            ).exists()
        )

    def test_activity_reject_refunds_simulated_publish_order(self):
        response = self.client.post(
            reverse("backoffice-activity-review", args=(self.handan_activity.id,)),
            {"decision": "reject", "reason": "活动信息不完整"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.handan_activity.refresh_from_db()
        order = self.handan_activity.publish_orders.get()
        self.assertEqual(self.handan_activity.status, Activity.Status.REJECTED)
        self.assertEqual(order.status, ActivityPublishOrder.Status.REFUNDED)
        self.assertEqual(response.data["data"]["rejection_reason"], "活动信息不完整")
        refund = ActivityRefundRecord.objects.get(publish_order=order)
        self.assertEqual(refund.refund_amount, 5280)
        self.assertEqual(refund.refund_type, ActivityRefundRecord.RefundType.REVIEW_REJECTION)

    def test_activity_review_permission_and_city_scope_are_required(self):
        self.role.permissions = ["activity.view"]
        self.role.save(update_fields=("permissions",))

        forbidden = self.client.post(
            reverse("backoffice-activity-review", args=(self.handan_activity.id,)),
            {"decision": "approve"},
            format="json",
        )
        outside_scope = self.client.get(
            reverse("backoffice-activity-detail", args=(self.beijing_activity.id,))
        )

        self.assertEqual(forbidden.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(outside_scope.status_code, status.HTTP_404_NOT_FOUND)

    def test_activity_category_management_configures_publish_boundaries(self):
        create_response = self.client.post(
            reverse("backoffice-activity-categories"),
            {
                "name": "密室",
                "slug": "escape-room-admin",
                "city_codes": ["130400", "130400"],
                "min_capacity": 4,
                "max_capacity": 12,
                "min_aa_principal_amount": 3000,
                "max_aa_principal_amount": 20000,
                "content_guidance": "须注明密室主题及安全提醒。",
                "sort_order": 20,
                "is_active": True,
            },
            format="json",
        )

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        data = create_response.data["data"]
        self.assertEqual(data["city_codes"], ["130400"])
        self.assertEqual(data["min_capacity"], 4)
        self.assertTrue(AdminAuditLog.objects.filter(
            action="activity_category.create", target_id=str(data["id"])
        ).exists())

        invalid = self.client.patch(
            reverse("backoffice-activity-category-detail", args=(data["id"],)),
            {"min_capacity": 20, "max_capacity": 10},
            format="json",
        )
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_cancel_without_participants_creates_refund_and_audit(self):
        activity = self.create_activity(
            organizer=self.organizer,
            title="可由后台取消的活动",
            city_code="130400",
            city_name="邯郸市",
        )
        activity.status = Activity.Status.RECRUITING
        activity.save(update_fields=("status", "updated_at"))

        response = self.client.post(
            reverse("backoffice-activity-action", args=(activity.id,)),
            {"action": "cancel", "reason": "集合场地临时关闭"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        activity.refresh_from_db()
        self.assertEqual(activity.status, Activity.Status.CANCELLED)
        self.assertEqual(activity.cancelled_by, self.admin_user)
        refund = activity.refund_records.get()
        self.assertEqual(refund.refund_type, ActivityRefundRecord.RefundType.ADMIN_CANCELLATION)
        self.assertEqual(response.data["data"]["refund_records"][0]["refund_amount"], 5280)
        self.assertTrue(AdminAuditLog.objects.filter(
            action="activity.management.cancel", target_id=str(activity.id)
        ).exists())

    def test_admin_cancel_refunds_active_participants_atomically(self):
        self.handan_activity.status = Activity.Status.RECRUITING
        self.handan_activity.save(update_fields=("status", "updated_at"))

        response = self.client.post(
            reverse("backoffice-activity-action", args=(self.handan_activity.id,)),
            {"action": "cancel", "reason": "运营处置测试"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.handan_activity.refresh_from_db()
        self.assertEqual(self.handan_activity.status, Activity.Status.CANCELLED)
        participant_refund = ActivityParticipationRefundOrder.objects.get(
            activity=self.handan_activity
        )
        self.assertEqual(participant_refund.refund_amount, 5280)
        self.assertEqual(
            participant_refund.refund_type,
            ActivityParticipationRefundOrder.RefundType.ADMIN_CANCELLATION,
        )

    def test_admin_cancel_is_blocked_when_participant_payment_is_missing(self):
        activity = self.create_activity(
            organizer=self.organizer,
            title="报名支付记录异常活动",
            city_code="130400",
            city_name="邯郸市",
        )
        activity.status = Activity.Status.RECRUITING
        activity.save(update_fields=("status", "updated_at"))
        ActivityParticipation.objects.create(
            activity=activity,
            user=self.participant,
            status=ActivityParticipation.Status.ACTIVE,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            joined_at=timezone.now(),
        )

        response = self.client.post(
            reverse("backoffice-activity-action", args=(activity.id,)),
            {"action": "cancel", "reason": "异常支付测试"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        activity.refresh_from_db()
        self.assertEqual(activity.status, Activity.Status.RECRUITING)
        self.assertFalse(activity.refund_records.exists())

    def test_user_report_can_be_processed_in_admin_with_city_scope(self):
        self.handan_activity.status = Activity.Status.RECRUITING
        self.handan_activity.save(update_fields=("status", "updated_at"))
        public_client = self.client_class()
        public_client.force_authenticate(self.participant)
        create_response = public_client.post(
            reverse("activity-report-create", args=(self.handan_activity.id,)),
            {"reason": "safety_risk", "description": "集合地点存在安全隐患"},
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        case_no = create_response.data["data"]["case_no"]

        list_response = self.client.get(reverse("backoffice-activity-reports"))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["data"]["summary"]["pending"], 1)
        self.assertEqual(list_response.data["data"]["items"][0]["case_no"], case_no)

        action_response = self.client.post(
            reverse("backoffice-activity-report-action", args=(case_no,)),
            {"action": "resolve", "result_note": "已核实并要求发起人整改"},
            format="json",
        )
        self.assertEqual(action_response.status_code, status.HTTP_200_OK)
        self.assertEqual(action_response.data["data"]["status"], ActivityReport.Status.RESOLVED)

    def test_rejected_organizer_can_fetch_copy_source(self):
        self.handan_activity.status = Activity.Status.REJECTED
        self.handan_activity.rejection_reason = "请补充参与规则"
        self.handan_activity.save(
            update_fields=("status", "rejection_reason", "updated_at")
        )
        public_client = self.client_class()
        public_client.force_authenticate(self.organizer)

        response = public_client.get(
            reverse("activity-copy-source", args=(self.handan_activity.id,))
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["title"], self.handan_activity.title)
        self.assertEqual(response.data["data"]["rejection_reason"], "请补充参与规则")

    def test_activity_finance_and_after_sales_review_create_traceable_refund(self):
        public_client = self.client_class()
        public_client.force_authenticate(self.participant)
        create_response = public_client.post(
            reverse("activity-after-sales", args=(self.handan_activity.id,)),
            {
                "reason": "illness_or_accident",
                "description": "突发身体不适，申请客服审核退款。",
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        case_no = create_response.data["data"]["case_no"]

        payment_list = self.client.get(
            reverse("backoffice-activity-finance"), {"record_type": "payment"}
        )
        case_list = self.client.get(
            reverse("backoffice-activity-finance"), {"record_type": "after_sales"}
        )
        self.assertEqual(payment_list.status_code, status.HTTP_200_OK)
        self.assertEqual(payment_list.data["data"]["summary"]["paid_count"], 1)
        self.assertEqual(case_list.data["data"]["items"][0]["case_no"], case_no)

        approved = self.client.post(
            reverse("backoffice-activity-after-sales-action", args=(case_no,)),
            {
                "action": "approve",
                "approved_principal_amount": 4800,
                "approved_service_fee_amount": 480,
                "result_note": "材料已核验，同意全额退款。",
            },
            format="json",
        )

        self.assertEqual(approved.status_code, status.HTTP_200_OK)
        case = ActivityAfterSalesCase.objects.get(case_no=case_no)
        self.assertEqual(case.status, ActivityAfterSalesCase.Status.APPROVED)
        self.assertEqual(case.refund_order.refund_amount, 5280)
        self.assertEqual(
            case.refund_order.status,
            ActivityParticipationRefundOrder.Status.SUCCEEDED,
        )
        self.assertTrue(AdminAuditLog.objects.filter(
            action="activity.after_sales.approve", target_id=case_no
        ).exists())
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.participant,
                event_type=UserNotification.EventType.ACTIVITY_AFTER_SALES_RESULT,
                target_id=str(self.handan_activity.id),
            ).exists()
        )

    def test_after_sales_freezes_settlement_and_admin_can_release_risk_freeze(self):
        now = timezone.now()
        self.handan_activity.status = Activity.Status.COMPLETED
        self.handan_activity.starts_at = now - timedelta(days=2, hours=3)
        self.handan_activity.ends_at = now - timedelta(days=2)
        self.handan_activity.formation_deadline = now - timedelta(days=3)
        self.handan_activity.save(update_fields=(
            "status", "starts_at", "ends_at", "formation_deadline", "updated_at",
        ))
        process_activity_timeouts(now=now)
        settlement = ActivitySettlement.objects.get(activity=self.handan_activity)
        self.assertEqual(settlement.status, ActivitySettlement.Status.RISK_FROZEN)

        public_client = self.client_class()
        public_client.force_authenticate(self.participant)
        created = public_client.post(
            reverse("activity-after-sales", args=(self.handan_activity.id,)),
            {
                "reason": "not_fulfilled",
                "description": "活动未按约定履行，申请平台核实处理。",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, ActivitySettlement.Status.DISPUTE_FROZEN)
        self.assertEqual(
            settlement.dispute_source, ActivitySettlement.DisputeSource.AFTER_SALES
        )

        settlement_list = self.client.get(
            reverse("backoffice-activity-finance"), {"record_type": "settlement"}
        )
        self.assertEqual(settlement_list.status_code, status.HTTP_200_OK)
        self.assertEqual(
            settlement_list.data["data"]["items"][0]["settlement_no"],
            settlement.settlement_no,
        )

        rejected = self.client.post(
            reverse(
                "backoffice-activity-after-sales-action",
                args=(created.data["data"]["case_no"],),
            ),
            {"action": "reject", "result_note": "材料不足，暂不支持本次退款申请。"},
            format="json",
        )
        self.assertEqual(rejected.status_code, status.HTTP_200_OK)
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, ActivitySettlement.Status.RISK_FROZEN)

        frozen = self.client.post(
            reverse(
                "backoffice-activity-settlement-action",
                args=(settlement.settlement_no,),
            ),
            {"action": "freeze_dispute", "reason": "风控复核发现线下争议信息"},
            format="json",
        )
        self.assertEqual(frozen.status_code, status.HTTP_200_OK)
        released = self.client.post(
            reverse(
                "backoffice-activity-settlement-action",
                args=(settlement.settlement_no,),
            ),
            {"action": "release_dispute"},
            format="json",
        )
        self.assertEqual(released.status_code, status.HTTP_200_OK)
        self.assertEqual(released.data["data"]["status"], "risk_frozen")
        self.assertTrue(AdminAuditLog.objects.filter(
            action="activity.settlement.release_dispute",
            target_id=settlement.settlement_no,
        ).exists())


class BackofficeSystemManagementTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            phone="19900007771",
            password="test-password",
            nickname="系统管理员",
        )
        cls.organization = Organization.objects.create(
            name="乐搭伴运营平台",
            code="system-management-platform",
            organization_type=Organization.Type.PLATFORM,
            city_codes=["130400", "110100"],
        )
        cls.admin_role = AdminRole.objects.create(
            organization=cls.organization,
            name="平台管理员",
            code="system-management-admin",
            permissions=["organization.manage", "audit.view", "dashboard.view"],
            data_scope=AdminRole.DataScope.ALL,
            is_system=True,
        )
        cls.admin_member = OrganizationMember.objects.create(
            user=cls.admin,
            organization=cls.organization,
            role=cls.admin_role,
        )
        cls.target_user = User.objects.create_user(
            phone="18800007772",
            password="test-password",
            nickname="新客服",
        )

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def test_role_member_management_and_audit_flow(self):
        catalog = self.client.get(reverse("backoffice-permissions"))
        self.assertEqual(catalog.status_code, status.HTTP_200_OK)
        permission_codes = {
            permission["code"]
            for group in catalog.data["data"]["groups"]
            for permission in group["permissions"]
        }
        self.assertIn("organization.manage", permission_codes)
        self.assertIn("order.after_sales.review", permission_codes)

        role_response = self.client.post(
            reverse("backoffice-roles"),
            {
                "organization": self.organization.id,
                "name": "客服专员",
                "code": "support-agent",
                "permissions": ["user.view", "order.after_sales.view"],
                "data_scope": AdminRole.DataScope.ORGANIZATION,
            },
            format="json",
        )
        self.assertEqual(role_response.status_code, status.HTTP_201_CREATED)
        role_id = role_response.data["data"]["id"]

        member_response = self.client.post(
            reverse("backoffice-members"),
            {
                "phone": self.target_user.phone,
                "organization": self.organization.id,
                "role": role_id,
                "city_codes": ["130400", "130400"],
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(member_response.status_code, status.HTTP_201_CREATED)
        member_id = member_response.data["data"]["id"]
        self.assertEqual(member_response.data["data"]["city_codes"], ["130400"])

        member_list = self.client.get(reverse("backoffice-members"))
        self.assertEqual(member_list.status_code, status.HTTP_200_OK)
        self.assertEqual(member_list.data["data"]["summary"]["total"], 2)

        updated = self.client.patch(
            reverse("backoffice-member-detail", args=(member_id,)),
            {"is_active": False, "city_codes": ["110100"]},
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        self.assertFalse(updated.data["data"]["is_active"])

        blocked_delete = self.client.delete(
            reverse("backoffice-role-detail", args=(role_id,))
        )
        self.assertEqual(blocked_delete.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(AdminAuditLog.objects.filter(
            action="system.role.create", target_id=str(role_id)
        ).exists())
        self.assertTrue(AdminAuditLog.objects.filter(
            action="system.member.update", target_id=str(member_id)
        ).exists())

    def test_non_platform_role_cannot_receive_global_operations_permission(self):
        city_organization = Organization.objects.create(
            name="测试城市运营中心",
            code="system-management-city",
            organization_type=Organization.Type.CITY_AGENT,
            city_codes=["130400"],
        )

        response = self.client.post(
            reverse("backoffice-roles"),
            {
                "organization": city_organization.id,
                "name": "违规全局运营角色",
                "code": "invalid-global-operator",
                "permissions": ["operations.manage"],
                "data_scope": AdminRole.DataScope.CITY,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("permissions", response.data)

    def test_non_platform_member_cannot_receive_legacy_global_role(self):
        city_organization = Organization.objects.create(
            name="旧角色城市运营中心",
            code="legacy-role-city",
            organization_type=Organization.Type.CITY_AGENT,
            city_codes=["130400"],
        )
        legacy_global_role = AdminRole.objects.create(
            organization=None,
            name="旧版全局角色",
            code="legacy-global-role",
            permissions=["organization.manage", "operations.manage"],
            data_scope=AdminRole.DataScope.ALL,
        )

        response = self.client.post(
            reverse("backoffice-members"),
            {
                "phone": self.target_user.phone,
                "organization": city_organization.id,
                "role": legacy_global_role.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("role", response.data)

    def test_system_role_self_protection_and_permission_validation(self):
        system_role_update = self.client.patch(
            reverse("backoffice-role-detail", args=(self.admin_role.id,)),
            {"name": "不可修改"},
            format="json",
        )
        self.assertEqual(system_role_update.status_code, status.HTTP_400_BAD_REQUEST)

        self_update = self.client.patch(
            reverse("backoffice-member-detail", args=(self.admin_member.id,)),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(self_update.status_code, status.HTTP_400_BAD_REQUEST)

        invalid_permission = self.client.post(
            reverse("backoffice-roles"),
            {
                "organization": self.organization.id,
                "name": "非法角色",
                "code": "invalid-role",
                "permissions": ["unknown.permission"],
                "data_scope": AdminRole.DataScope.ORGANIZATION,
            },
            format="json",
        )
        self.assertEqual(invalid_permission.status_code, status.HTTP_400_BAD_REQUEST)

    def test_organization_manage_permission_is_required(self):
        viewer = User.objects.create_user(
            phone="19900007773",
            password="test-password",
            nickname="只读人员",
        )
        viewer_role = AdminRole.objects.create(
            organization=self.organization,
            name="看板人员",
            code="dashboard-only-role",
            permissions=["dashboard.view"],
            data_scope=AdminRole.DataScope.ORGANIZATION,
        )
        OrganizationMember.objects.create(
            user=viewer,
            organization=self.organization,
            role=viewer_role,
        )
        self.client.force_authenticate(viewer)
        self.assertEqual(
            self.client.get(reverse("backoffice-roles")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get(reverse("backoffice-members")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
