from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from orders.models import CouponTemplate, ProviderOrder
from providers.models import ProviderProfile, ProviderService, ServiceCategory

from .models import GrowthCampaignConfig, Invitation, NewcomerGiftGrant, NewcomerGiftItem
from .services import award_first_order_reward, register_invited_user


@override_settings(DEBUG=True)
class GrowthCampaignTests(TestCase):
    def setUp(self):
        self.inviter = User.objects.create_user(
            phone="13800007101", password="test-pass-123", nickname="邀请人"
        )
        self.registration_template = CouponTemplate.objects.create(
            name="注册无门槛券", face_amount=1500, min_order_amount=0, valid_days=30
        )
        self.first_order_template = CouponTemplate.objects.create(
            name="首单大额券", face_amount=5000, min_order_amount=19900, valid_days=30
        )
        self.gift_template_one = CouponTemplate.objects.create(
            name="新人无门槛券", face_amount=1000, min_order_amount=0, valid_days=30
        )
        self.gift_template_two = CouponTemplate.objects.create(
            name="新人满减券", face_amount=2000, min_order_amount=9900, valid_days=30
        )
        self.config = GrowthCampaignConfig.objects.create(
            pk=1,
            newcomer_gift_enabled=True,
            invitation_enabled=True,
            registration_reward_template=self.registration_template,
            first_order_reward_template=self.first_order_template,
        )
        NewcomerGiftItem.objects.create(
            config=self.config, template=self.gift_template_one, position=0
        )
        NewcomerGiftItem.objects.create(
            config=self.config, template=self.gift_template_two, position=1
        )

    def test_registration_endpoint_binds_invitation_and_issues_both_sides(self):
        phone = "13800007102"
        self.client.post(
            "/api/v1/auth/sms-codes/",
            {"phone": phone, "purpose": "register"},
            content_type="application/json",
        )
        response = self.client.post(
            "/api/v1/auth/register/",
            {
                "phone": phone,
                "code": "123456",
                "password": "test-pass-123",
                "invite_code": str(self.inviter.public_id),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        invitee = User.objects.get(phone=phone)
        invitation = Invitation.objects.get(invitee=invitee)
        self.assertEqual(invitation.inviter, self.inviter)
        self.assertIsNotNone(invitation.registration_reward_coupon_id)
        self.assertEqual(invitee.coupons.filter(source="newcomer_gift").count(), 2)
        self.assertEqual(self.inviter.coupons.filter(source="invite_registration").count(), 1)
        self.assertEqual(NewcomerGiftGrant.objects.get(recipient=invitee).coupons.count(), 2)

    def test_registration_reward_is_idempotent(self):
        invitee = User.objects.create_user(phone="13800007103", password="test-pass-123")
        register_invited_user(user=invitee, invite_code=self.inviter.public_id)
        register_invited_user(user=invitee, invite_code=self.inviter.public_id)
        self.assertEqual(Invitation.objects.filter(invitee=invitee).count(), 1)
        self.assertEqual(invitee.coupons.filter(source="newcomer_gift").count(), 2)
        self.assertEqual(self.inviter.coupons.filter(source="invite_registration").count(), 1)

    def test_first_completed_order_reward_is_idempotent(self):
        invitee = User.objects.create_user(phone="13800007104", password="test-pass-123")
        register_invited_user(user=invitee, invite_code=self.inviter.public_id)
        provider_user = User.objects.create_user(phone="13800007105", password="test-pass-123")
        provider = ProviderProfile.objects.create(
            user=provider_user,
            status=ProviderProfile.Status.APPROVED,
            onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
            identity_status=ProviderProfile.IdentityStatus.VERIFIED,
            display_name="测试达人",
            service_city_code="130400",
            service_city_name="邯郸市",
            bio="测试达人简介",
        )
        category = ServiceCategory.objects.create(name="测试服务", slug="growth-test")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=20000,
        )
        now = timezone.now()
        order = ProviderOrder.objects.create(
            order_no="DZYGROWTH0001",
            customer=invitee,
            provider=provider,
            service=service,
            provider_name_snapshot="测试达人",
            service_name_snapshot="测试服务",
            billing_type_snapshot=ProviderOrder.BillingType.PER_SESSION,
            unit_price_amount=20000,
            starts_at=now - timedelta(hours=2),
            ends_at=now - timedelta(hours=1),
            duration_minutes=60,
            meeting_address="测试地址",
            contact_name="测试用户",
            contact_phone=invitee.phone,
            service_fee_amount=20000,
            payable_amount=20000,
            pricing_snapshot={},
            status=ProviderOrder.Status.COMPLETED,
            payment_expires_at=now - timedelta(days=1),
            paid_at=now - timedelta(days=2),
            customer_confirmed_at=now,
        )
        award_first_order_reward(order_id=order.pk)
        award_first_order_reward(order_id=order.pk)
        invitation = Invitation.objects.get(invitee=invitee)
        self.assertEqual(invitation.first_completed_order, order)
        self.assertIsNotNone(invitation.first_order_reward_coupon_id)
        self.assertEqual(self.inviter.coupons.filter(source="invite_first_order").count(), 1)

    def test_public_campaign_rejects_malformed_invite_code_without_error(self):
        response = self.client.get("/api/v1/growth/campaign/?invite_code=not-a-uuid")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["invitation_valid"])

    def test_admin_can_update_growth_configuration_and_read_records(self):
        self.inviter.is_superuser = True
        self.inviter.is_staff = True
        self.inviter.save(update_fields=("is_superuser", "is_staff"))
        self.client.force_login(self.inviter)
        response = self.client.patch(
            "/api/v1/admin/growth/config/",
            {
                "newcomer_gift_enabled": True,
                "invitation_enabled": True,
                "newcomer_gift_template_public_ids": [
                    str(self.gift_template_two.public_id),
                    str(self.gift_template_one.public_id),
                ],
                "registration_reward_template_public_id": str(
                    self.registration_template.public_id
                ),
                "first_order_reward_template_public_id": str(
                    self.first_order_template.public_id
                ),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["public_id"] for item in response.json()["data"]["newcomer_gift_templates"]],
            [str(self.gift_template_two.public_id), str(self.gift_template_one.public_id)],
        )
        records = self.client.get("/api/v1/admin/growth/invitations/")
        self.assertEqual(records.status_code, 200)
        self.assertEqual(records.json()["data"]["pagination"]["total"], 0)
