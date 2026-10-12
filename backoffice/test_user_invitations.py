import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from growth.models import Invitation
from orders.models import ProviderOrder, ProviderOrderPaymentOrder, ProviderOrderRefundOrder
from providers.models import ProviderProfile, ProviderService, ServiceCategory

from .models import AdminRole, Organization, OrganizationMember


class UserInvitationStatisticsTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(phone="19900009100", is_staff=True, is_superuser=True)
        cls.owner = User.objects.create_user(phone="19900009101", nickname="邀请人")
        cls.first = User.objects.create_user(phone="19900009102", nickname="用户甲")
        cls.second = User.objects.create_user(phone="19900009103", nickname="用户乙")
        cls.empty = User.objects.create_user(phone="19900009104", nickname="尚未下单")
        cls.upper = User.objects.create_user(phone="19900009105", nickname="上级邀请人")
        cls.grandchild = User.objects.create_user(phone="19900009106", nickname="间接邀请用户")
        provider_user = User.objects.create_user(phone="19900009107", nickname="接单达人")
        cls.provider = ProviderProfile.objects.create(
            user=provider_user, status="approved", service_city_code="130400"
        )
        category = ServiceCategory.objects.create(name="邀请统计测试", slug="invite-statistics")
        cls.service = ProviderService.objects.create(
            provider=cls.provider, category=category, billing_type="per_session", price_amount=10000
        )
        for inviter, invitee in (
            (cls.upper, cls.owner),
            (cls.owner, cls.first),
            (cls.owner, cls.second),
            (cls.owner, cls.empty),
            (cls.first, cls.grandchild),
        ):
            Invitation.objects.create(
                inviter=inviter,
                invitee=invitee,
                invite_code_snapshot=str(inviter.public_id),
                registered_at=timezone.now(),
            )

    def setUp(self):
        self.client.force_authenticate(self.admin)
        self.url = reverse("backoffice-user-invitations", args=(self.owner.public_id,))

    def order(
        self,
        user,
        at="2026-10-12T12:00:00+08:00",
        *,
        status="completed",
        paid=True,
        payment_status="paid",
        service=None,
    ):
        created = datetime.fromisoformat(at)
        service = service or self.service
        order = ProviderOrder.objects.create(
            order_no=f"INV{uuid.uuid4().hex[:20]}",
            customer=user,
            provider=service.provider,
            service=service,
            provider_name_snapshot="达人",
            service_name_snapshot="服务",
            billing_type_snapshot="per_session",
            unit_price_amount=10000,
            service_fee_amount=10000,
            payable_amount=10000,
            starts_at=created + timedelta(days=5),
            ends_at=created + timedelta(days=5, hours=1),
            duration_minutes=60,
            contact_name="测试",
            contact_phone=user.phone,
            payment_expires_at=created + timedelta(minutes=30),
            paid_at=created + timedelta(minutes=1) if paid else None,
            status=status,
        )
        ProviderOrder.objects.filter(pk=order.pk).update(created_at=created)
        if payment_status:
            ProviderOrderPaymentOrder.objects.create(
                order=order,
                payer=user,
                service_fee_amount=10000,
                transport_fee_amount=0,
                other_fee_amount=0,
                discount_amount=0,
                payable_amount=10000,
                status=payment_status,
                paid_at=order.paid_at,
                channel="balance",
                expires_at=order.payment_expires_at,
            )
        return order

    def refund(self, order, amount, status="succeeded"):
        return ProviderOrderRefundOrder.objects.create(
            order=order,
            payment_order=order.payment_order,
            beneficiary=order.customer,
            source_type="admin",
            source_reference=uuid.uuid4().hex,
            idempotency_key=uuid.uuid4().hex,
            service_fee_refund_amount=amount,
            transport_fee_refund_amount=0,
            other_fee_refund_amount=0,
            refund_amount=amount,
            wallet_refund_amount=amount,
            external_refund_amount=0,
            status=status,
            reason="测试退款",
        )

    def get_data(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_inviter_and_only_direct_invitees_are_visible_and_masked(self):
        self.order(self.grandchild)
        self.order(self.owner)
        self.order(self.first, "2026-10-11T12:00:00+08:00")
        self.order(self.second)
        data = self.get_data()
        self.assertEqual(data["inviter"]["public_id"], str(self.upper.public_id))
        self.assertEqual(data["inviter"]["phone_masked"], "199****9105")
        self.assertEqual(
            [i["public_id"] for i in data["items"]],
            [str(u.public_id) for u in (self.second, self.first, self.empty)],
        )
        self.assertEqual(
            data["summary"], {"invitee_count": 3, "ordering_user_count": 2, "order_count": 2}
        )
        self.assertEqual(data["items"][-1]["order_count"], 0)
        self.assertIsNone(data["items"][-1]["latest_order_at"])
        self.assertNotIn(self.first.phone, str(data))

    def test_empty_relationship_is_not_an_error(self):
        self.url = reverse("backoffice-user-invitations", args=(self.provider.user.public_id,))
        data = self.get_data()
        self.assertIsNone(data["inviter"])
        self.assertFalse(data["inviter_restricted"])
        self.assertEqual(data["items"], [])
        self.assertEqual(data["summary"]["order_count"], 0)

    def test_excludes_unpaid_cancelled_and_refunded_but_keeps_partial_and_legacy_paid(self):
        for state in (
            "pending_acceptance",
            "pending_service",
            "departed",
            "in_service",
            "pending_confirmation",
            "pending_review",
            "completed",
            "terminated",
            "after_sales",
            "pending_support",
        ):
            self.order(self.first, status=state)
        self.order(self.first, status="completed", payment_status=None)  # Historical paid row.
        self.order(self.first, status="after_sales", payment_status="partially_refunded")
        for state in ("pending_payment", "cancelled", "refunded"):
            self.order(self.first, status=state)
        self.order(self.first, paid=False)
        for state in ("pending_payment", "closed", "refunded"):
            self.order(self.first, payment_status=state)
        data = self.get_data()
        self.assertEqual(data["summary"]["order_count"], 12)
        self.assertEqual(data["items"][0]["order_count"], 12)

    def test_successful_refunds_are_summed_without_duplicating_order_counts(self):
        full = self.order(self.first)
        self.refund(full, 4000)
        self.refund(full, 6000)
        partial = self.order(self.first, payment_status="partially_refunded")
        self.refund(partial, 1000)
        self.refund(partial, 2000)
        self.refund(partial, 7000, "failed")
        self.refund(partial, 7000, "processing")
        self.refund(partial, 7000, "pending")
        data = self.get_data()
        self.assertEqual(data["summary"]["order_count"], 1)
        self.assertEqual(data["items"][0]["order_count"], 1)

    def test_dates_use_order_creation_and_include_the_entire_shanghai_end_date(self):
        for at in (
            "2026-10-10T15:59:59+00:00",
            "2026-10-10T16:00:00+00:00",
            "2026-10-11T15:59:59.999999+00:00",
            "2026-10-11T16:00:00+00:00",
        ):
            self.order(self.first, at)
        # An order paid in the range but created outside it must not count.
        old = self.order(self.second, "2026-10-09T12:00:00+08:00")
        ProviderOrder.objects.filter(pk=old.pk).update(
            paid_at=datetime(2026, 10, 11, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        with timezone.override("UTC"):
            data = self.get_data(date_from="2026-10-11", date_to="2026-10-11")
        self.assertEqual(data["summary"]["order_count"], 2)
        self.assertEqual(
            data["items"][0]["latest_order_at"],
            datetime.fromisoformat("2026-10-11T15:59:59.999999+00:00"),
        )
        self.assertEqual(self.get_data(date_from="2026-10-12")["summary"]["order_count"], 1)
        self.assertEqual(self.get_data(date_to="2026-10-10")["summary"]["order_count"], 2)

    def test_sort_uses_filtered_latest_order_and_totals_ignore_pagination(self):
        self.order(self.first, "2026-10-01T12:00:00+08:00")
        self.order(self.first, "2026-10-12T12:00:00+08:00")
        self.order(self.second, "2026-10-05T12:00:00+08:00")
        params = {"date_from": "2026-10-01", "date_to": "2026-10-10", "page_size": 1}
        first, second, third = [self.get_data(**params, page=p) for p in (1, 2, 3)]
        self.assertEqual(first["items"][0]["public_id"], str(self.second.public_id))
        self.assertEqual(second["items"][0]["public_id"], str(self.first.public_id))
        self.assertEqual(third["items"][0]["order_count"], 0)
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(second["summary"], third["summary"])
        self.assertEqual(third["summary"]["order_count"], 2)
        self.assertEqual(self.get_data(page=99)["items"], [])
        self.assertEqual(self.get_data()["items"][0]["public_id"], str(self.first.public_id))

    def test_invalid_filters_are_rejected(self):
        for params in (
            {"date_from": "bad"},
            {"date_from": "0001-01-01"},
            {"date_to": "2026-02-30"},
            {"date_from": "2026-10-12", "date_to": "2026-10-11"},
            {"date_to": "9999-12-31"},
            {"page": 0},
            {"page_size": 101},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.url, params).status_code, 400)

    def test_authentication_permission_and_user_visibility(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.url).status_code, (401, 403))
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_authenticate(self.admin)
        for public_id in (uuid.uuid4(), self.admin.public_id):
            url = reverse("backoffice-user-invitations", args=(public_id,))
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_city_scope_filters_users_orders_inviter_and_totals(self):
        self.order(self.owner)
        self.order(self.first)
        outside_user = User.objects.create_user(phone="19900009108")
        outside = ProviderProfile.objects.create(user=outside_user, service_city_code="110100")
        outside_service = ProviderService.objects.create(
            provider=outside,
            category=self.service.category,
            billing_type="per_session",
            price_amount=10000,
        )
        self.order(self.first, service=outside_service)
        self.order(self.second, service=outside_service)
        org = Organization.objects.create(
            name="城市运营",
            code="invitation-city",
            organization_type="city_agent",
            city_codes=["130400"],
        )
        role = AdminRole.objects.create(
            organization=org,
            name="用户查看",
            code="users",
            permissions=["user.view"],
            data_scope="city",
        )
        city_admin = User.objects.create_user(phone="19900009109")
        OrganizationMember.objects.create(user=city_admin, organization=org, role=role)
        self.client.force_authenticate(city_admin)
        data = self.get_data()
        self.assertEqual(data["data_scope"], "cities")
        self.assertIsNone(data["inviter"])
        self.assertTrue(data["inviter_restricted"])
        self.assertNotIn(str(self.upper.public_id), str(data))
        self.assertNotIn(str(self.second.public_id), str(data))
        self.assertEqual(
            data["summary"], {"invitee_count": 1, "ordering_user_count": 1, "order_count": 1}
        )
        self.assertEqual(data["items"][0]["order_count"], 1)
        self.url = reverse("backoffice-user-invitations", args=(self.second.public_id,))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        role.permissions = []
        role.save(update_fields=["permissions"])
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_query_count_does_not_grow_with_page_size(self):
        with CaptureQueriesContext(connection) as small:
            self.get_data(page_size=1)
        with CaptureQueriesContext(connection) as large:
            self.get_data(page_size=100)
        self.assertEqual(len(small), len(large))
        self.assertLessEqual(len(large), 8)
