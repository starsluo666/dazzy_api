from django.urls import reverse
from datetime import timedelta
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from backoffice.models import AdminAuditLog, AdminRole, Organization, OrganizationMember
from mediafiles.models import MediaAsset
from notifications.models import UserNotification
from providers.models import ProviderProfile
from providers.models import ProviderService, ServiceCategory
from orders.models import ProviderOrder, UserCoupon

from .models import SupportCase, SupportCaseRecord


class SupportCaseApiTests(APITestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            phone="18800001111", nickname="反馈用户"
        )
        provider_user = User.objects.create_user(
            phone="18800002222", nickname="测试达人"
        )
        self.provider = ProviderProfile.objects.create(
            user=provider_user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        self.attachment = MediaAsset.objects.create(
            owner=self.customer,
            scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.SUPPORT_ATTACHMENT,
            status=MediaAsset.Status.UPLOADED,
            object_key="dazzy-test/private/support/test.webp",
            content_type="image/webp",
            size_bytes=1024,
            uploaded_at=timezone.now(),
        )
        self.client.force_authenticate(self.customer)

    def create_provider_report(self):
        return self.client.post(
            reverse("support-case-list"),
            {
                "case_type": "report",
                "target_type": "provider",
                "target_id": str(self.provider.user.public_id),
                "reason": "private_transaction",
                "description": "达人要求添加私人联系方式并在线下单。",
                "attachment_ids": [str(self.attachment.pk)],
            },
            format="json",
        )

    def test_create_report_with_private_attachment_and_deduplicate(self):
        response = self.create_provider_report()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["created"])
        self.assertEqual(response.data["data"]["city_code"], "130400")
        self.assertEqual(len(response.data["data"]["attachment_urls"]), 1)
        case = SupportCase.objects.get()
        self.assertEqual(case.attachments.get(), self.attachment)
        self.assertEqual(case.records.get().record_type, SupportCaseRecord.RecordType.CREATED)

        duplicate = self.client.post(
            reverse("support-case-list"),
            {
                "case_type": "report",
                "target_type": "provider",
                "target_id": str(self.provider.user.public_id),
                "reason": "safety_risk",
                "description": "再次提交同一个达人的举报说明。",
            },
            format="json",
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertFalse(duplicate.data["created"])
        self.assertEqual(duplicate.data["data"]["case_no"], case.case_no)
        self.assertEqual(SupportCase.objects.count(), 1)

    def test_non_order_feedback_types_share_existing_open_case(self):
        first = self.create_provider_report()
        response = self.client.post(reverse("support-case-list"), {
            "case_type": "complaint", "target_type": "provider",
            "target_id": str(self.provider.user.public_id),
            "reason": "service_quality", "description": "同一达人已有进行中的工单。",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["created"])
        self.assertEqual(response.data["data"]["case_no"], first.data["data"]["case_no"])
        self.assertEqual(SupportCase.objects.count(), 1)

    def test_attachment_must_belong_to_current_user(self):
        stranger = User.objects.create_user(phone="18800003333", nickname="其他用户")
        self.attachment.owner = stranger
        self.attachment.save(update_fields=("owner",))
        response = self.create_provider_report()
        self.assertEqual(response.status_code, 400)
        self.assertIn("attachment_ids", response.data)

    def test_cases_are_private_and_user_can_reply(self):
        case_no = self.create_provider_report().data["data"]["case_no"]
        response = self.client.post(
            reverse("support-case-reply", args=(case_no,)),
            {"content": "补充说明：事情发生在今晚八点左右。"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["data"]["records"]), 2)

        stranger = User.objects.create_user(phone="18800004444", nickname="无关用户")
        self.client.force_authenticate(stranger)
        detail = self.client.get(reverse("support-case-detail", args=(case_no,)))
        self.assertEqual(detail.status_code, 404)

    def test_resolved_case_can_request_review_only_once(self):
        case_no = self.create_provider_report().data["data"]["case_no"]
        case = SupportCase.objects.get(case_no=case_no)
        case.status = SupportCase.Status.RESOLVED
        case.result_note = "已完成首次核查。"
        case.resolved_at = timezone.now()
        case.save(update_fields=("status", "result_note", "resolved_at", "updated_at"))

        response = self.client.post(
            reverse("support-case-review", args=(case_no,)),
            {"reason": "我有新的证据，希望平台再次核查。"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], SupportCase.Status.REVIEWING)
        case.status = SupportCase.Status.REJECTED
        case.save(update_fields=("status", "updated_at"))
        repeated = self.client.post(
            reverse("support-case-review", args=(case_no,)),
            {"reason": "再次申请复核不应被接受。"},
            format="json",
        )
        self.assertEqual(repeated.status_code, 400)


class AdminSupportCaseApiTests(APITestCase):
    def setUp(self):
        reporter = User.objects.create_user(phone="18810001111", nickname="投诉用户")
        provider_user = User.objects.create_user(phone="18810002222", nickname="邯郸达人")
        provider = ProviderProfile.objects.create(
            user=provider_user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        self.case = SupportCase.objects.create(
            reporter=reporter,
            case_type=SupportCase.CaseType.COMPLAINT,
            target_type=SupportCase.TargetType.PROVIDER,
            provider=provider,
            reason=SupportCase.Reason.SERVICE_QUALITY,
            description="线下服务体验与页面承诺不一致。",
            city_code="130400",
            city_name="邯郸市",
        )
        organization = Organization.objects.create(
            name="邯郸运营中心",
            code="handan-support",
            organization_type=Organization.Type.CITY_AGENT,
            city_codes=["130400"],
        )
        role = AdminRole.objects.create(
            organization=organization,
            name="客服专员",
            code="support-agent",
            permissions=["support.case.view", "support.case.manage"],
            data_scope=AdminRole.DataScope.CITY,
        )
        self.operator = User.objects.create_user(
            phone="19910001111", nickname="客服小乐"
        )
        OrganizationMember.objects.create(
            user=self.operator, organization=organization, role=role
        )
        self.client.force_authenticate(self.operator)

    def test_operator_can_process_reply_and_resolve_with_audit(self):
        listing = self.client.get(reverse("admin-support-cases"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["data"]["summary"]["pending"], 1)

        start = self.client.post(
            reverse("admin-support-case-action", args=(self.case.case_no,)),
            {"action": "start_review"},
            format="json",
        )
        self.assertEqual(start.status_code, 200)
        self.assertEqual(start.data["data"]["status"], SupportCase.Status.PROCESSING)

        reply = self.client.post(
            reverse("admin-support-case-reply", args=(self.case.case_no,)),
            {"content": "您好，客服已开始核查相关服务记录。"},
            format="json",
        )
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.case.reporter,
                event_type=UserNotification.EventType.SUPPORT_REPLY,
            ).count(),
            1,
        )

        resolved = self.client.post(
            reverse("admin-support-case-action", args=(self.case.case_no,)),
            {"action": "resolve", "result_note": "核查属实，已对达人进行提醒教育。"},
            format="json",
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.data["data"]["status"], SupportCase.Status.RESOLVED)
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.case.reporter,
                event_type=UserNotification.EventType.SUPPORT_RESULT,
            ).count(),
            1,
        )
        self.assertEqual(
            AdminAuditLog.objects.filter(
                target_id=self.case.case_no, action="support.case.resolve"
            ).count(),
            1,
        )

    def test_customer_service_can_find_case_reporter_and_issue_coupon(self):
        search = self.client.get(reverse("backoffice-coupons"), {"search": "投诉用户"})
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.data["data"]["users"][0]["public_id"], str(self.case.reporter.public_id))
        issued = self.client.post(
            reverse("backoffice-coupons"),
            {"user_public_id": str(self.case.reporter.public_id)}, format="json",
        )
        self.assertEqual(issued.status_code, 201)
        self.assertEqual(issued.data["data"]["face_amount"], 2000)
        self.assertEqual(UserCoupon.objects.filter(owner=self.case.reporter).count(), 1)
        self.assertTrue(AdminAuditLog.objects.filter(action="coupon.issue").exists())

    def test_coupon_endpoints_reject_invalid_user_identifier(self):
        url = reverse("backoffice-coupons")
        listing = self.client.get(url, {"user_public_id": "invalid-uuid"})
        self.assertEqual(listing.status_code, 400)
        issued = self.client.post(url, {"user_public_id": "invalid-uuid"}, format="json")
        self.assertEqual(issued.status_code, 400)
        self.assertFalse(UserCoupon.objects.exists())

    def test_city_scoped_operator_cannot_view_other_city(self):
        self.case.city_code = "110100"
        self.case.city_name = "北京市"
        self.case.save(update_fields=("city_code", "city_name", "updated_at"))
        listing = self.client.get(reverse("admin-support-cases"))
        self.assertEqual(listing.data["data"]["pagination"]["total"], 0)
        detail = self.client.get(
            reverse("admin-support-case-detail", args=(self.case.case_no,))
        )
        self.assertEqual(detail.status_code, 404)

    def test_terminal_status_group_matches_completed_summary(self):
        for status in (
            SupportCase.Status.RESOLVED,
            SupportCase.Status.REJECTED,
            SupportCase.Status.CLOSED,
        ):
            SupportCase.objects.create(
                reporter=self.case.reporter,
                case_type=SupportCase.CaseType.CONSULTATION,
                target_type=SupportCase.TargetType.GENERAL,
                reason=SupportCase.Reason.OTHER,
                description=f"用于验证{status}状态归入已完结筛选。",
                city_code="130400",
                city_name="邯郸市",
                status=status,
            )
        response = self.client.get(
            reverse("admin-support-cases"), {"status_group": "terminal"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["summary"]["resolved"], 3)
        self.assertEqual(response.data["data"]["pagination"]["total"], 3)

    def test_review_resolution_creates_review_result_notification(self):
        self.case.status = SupportCase.Status.REVIEWING
        self.case.review_requested_at = timezone.now()
        self.case.review_reason = "用户补充了新的沟通记录。"
        self.case.save(
            update_fields=(
                "status",
                "review_requested_at",
                "review_reason",
                "updated_at",
            )
        )
        response = self.client.post(
            reverse("admin-support-case-action", args=(self.case.case_no,)),
            {"action": "resolve", "result_note": "复核完成，维持原处理结论。"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        notification = UserNotification.objects.get(recipient=self.case.reporter)
        self.assertEqual(
            notification.event_type,
            UserNotification.EventType.SUPPORT_REVIEW_RESULT,
        )
        self.assertEqual(notification.title, "工单复核已完成")

    def test_reward_report_requires_order_evidence_and_issues_coupon_once(self):
        now = timezone.now()
        category = ServiceCategory.objects.create(name="举报测试服务", slug="reward-report-test")
        service = ProviderService.objects.create(
            provider=self.case.provider, category=category,
            billing_type=ProviderService.BillingType.PER_SESSION, price_amount=20000,
        )
        order = ProviderOrder.objects.create(
            order_no="REWARD-ORDER-001", customer=self.case.reporter,
            provider=self.case.provider, service=service,
            provider_name_snapshot="邯郸达人", service_name_snapshot="举报测试服务",
            billing_type_snapshot=ProviderOrder.BillingType.PER_SESSION,
            unit_price_amount=20000, starts_at=now - timedelta(hours=4),
            ends_at=now - timedelta(hours=2), duration_minutes=120,
            meeting_address="邯郸测试地点", contact_name="测试用户", contact_phone="13812346688",
            service_fee_amount=20000, payable_amount=20000,
            status=ProviderOrder.Status.PENDING_REVIEW,
            payment_expires_at=now - timedelta(days=2),
            customer_confirmed_at=now, review_expires_at=now + timedelta(days=7),
        )
        attachment = MediaAsset.objects.create(
            owner=self.case.reporter, scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.SUPPORT_ATTACHMENT,
            status=MediaAsset.Status.UPLOADED,
            object_key="dazzy-test/private/support/reward.webp",
            content_type="image/webp", size_bytes=1024, uploaded_at=now,
        )
        self.client.force_authenticate(self.case.reporter)
        feedback = self.client.post(reverse("support-case-list"), {
            "case_type": "consultation", "target_type": "provider_order",
            "target_id": order.order_no, "reason": "platform_process",
            "description": "反馈这个订单遇到的平台流程问题。",
        }, format="json")
        self.assertEqual(feedback.status_code, 201)
        payload = {
            "case_type": "report", "target_type": "provider_order",
            "target_id": order.order_no, "reason": "safety_risk",
            "description": "达人服务过程中存在安全风险，请平台核查。",
            "reward_eligible": True,
        }
        missing_evidence = self.client.post(reverse("support-case-list"), payload, format="json")
        self.assertEqual(missing_evidence.status_code, 400)
        payload["attachment_ids"] = [str(attachment.pk)]
        report = self.client.post(reverse("support-case-list"), payload, format="json")
        self.assertEqual(report.status_code, 201)
        case_no = report.data["data"]["case_no"]
        self.client.force_authenticate(self.operator)
        resolved = self.client.post(
            reverse("admin-support-case-action", args=(case_no,)),
            {"action": "resolve", "result_note": "举报证据属实，已完成违规核查。"}, format="json",
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertTrue(resolved.data["data"]["reward_issued"])
        coupon = UserCoupon.objects.get(owner=self.case.reporter, source="report_reward")
        self.assertEqual(coupon.face_amount, 2000)
        self.assertEqual(coupon.min_order_amount, 10000)
        repeated = self.client.post(
            reverse("admin-support-case-action", args=(case_no,)),
            {"action": "resolve", "result_note": "再次处理不应发券。"}, format="json",
        )
        self.assertEqual(repeated.status_code, 400)
        self.assertEqual(UserCoupon.objects.filter(owner=self.case.reporter, source="report_reward").count(), 1)
