from importlib import import_module
from io import StringIO
from types import SimpleNamespace

from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from accounts.models import User
from backoffice.models import AdminRole, Organization, OrganizationMember
from orders.models import CouponCampaign, UserCoupon


repair = import_module("backoffice.migrations.0039_grant_coupon_campaign_permissions")
CAMPAIGN_PERMISSIONS = {"coupon_campaign.view", "coupon_campaign.manage"}


class CouponCampaignPermissionTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(phone="13977114000", password="test-password")
        cls.platform = Organization.objects.create(
            name="测试平台", code="dazzy-platform", organization_type="platform",
        )

    def make_role(self, **overrides):
        return AdminRole.objects.create(**{
            "organization": self.platform,
            "code": "platform-admin",
            "name": "平台管理员",
            "is_system": True,
            "data_scope": "all",
            "permissions": ["dashboard.view", "coupon.view", "provider_invite.view"],
            **overrides,
        })

    def apply_repair(self):
        # Use the historical model registry, as Django does when upgrading an old DB.
        historical_apps = MigrationLoader(connection).project_state(
            [("backoffice", "0038_provider_invite_permissions")]
        ).apps
        repair.grant_platform_role(historical_apps, SimpleNamespace(connection=connection))

    def session_permissions(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/v1/admin/me/")
        self.assertEqual(response.status_code, 200, response.data)
        return set(response.data["data"]["permissions"])

    def test_command_grants_new_platform_admin_campaign_access(self):
        call_command("grant_platform_admin", self.user.phone, stdout=StringIO())
        role = AdminRole.objects.get(code="platform-admin", organization=self.platform)
        self.assertTrue(CAMPAIGN_PERMISSIONS <= set(role.permissions))
        self.assertTrue(CAMPAIGN_PERMISSIONS <= self.session_permissions())
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(self.client.get("/api/v1/admin/coupon-campaigns/").status_code, 200)
        # Empty input reaches validation, rather than being rejected for missing manage permission.
        self.assertEqual(self.client.post("/api/v1/admin/coupon-campaigns/", {}, format="json").status_code, 400)
        self.assertFalse(CouponCampaign.objects.exists())
        self.assertFalse(UserCoupon.objects.exists())

    def test_command_rerun_does_not_remove_campaign_permissions(self):
        call_command("grant_platform_admin", self.user.phone, stdout=StringIO())
        call_command("grant_platform_admin", self.user.phone, stdout=StringIO())
        self.assertEqual(AdminRole.objects.count(), 1)
        self.assertEqual(OrganizationMember.objects.count(), 1)
        role = AdminRole.objects.get()
        for permission in CAMPAIGN_PERMISSIONS:
            self.assertEqual(role.permissions.count(permission), 1)

    def test_upgrade_adds_only_missing_grants_and_immediately_exposes_menu_permission(self):
        role = self.make_role()
        original = list(role.permissions)
        member = OrganizationMember.objects.create(user=self.user, organization=self.platform, role=role)
        self.assertTrue(CAMPAIGN_PERMISSIONS.isdisjoint(self.session_permissions()))
        self.assertEqual(self.client.get("/api/v1/admin/coupon-campaigns/").status_code, 403)
        self.apply_repair()
        role.refresh_from_db()
        self.assertEqual(role.permissions, original + list(repair.PERMISSIONS))
        self.assertTrue(CAMPAIGN_PERMISSIONS <= self.session_permissions())
        self.assertEqual(self.client.get("/api/v1/admin/coupon-campaigns/").status_code, 200)
        member.refresh_from_db()
        self.assertEqual(member.role_id, role.pk)
        self.assertEqual(role.data_scope, "all")
        self.assertTrue(role.is_system)

    def test_upgrade_handles_empty_partial_and_already_granted_roles_idempotently(self):
        for initial in [[], ["coupon_campaign.view"], list(repair.PERMISSIONS)]:
            with self.subTest(initial=initial):
                role = self.make_role(permissions=initial)
                self.apply_repair()
                role.refresh_from_db()
                self.assertEqual(set(role.permissions), CAMPAIGN_PERMISSIONS)
                expected = list(role.permissions)
                with CaptureQueriesContext(connection) as queries:
                    self.apply_repair()
                role.refresh_from_db()
                self.assertEqual(role.permissions, expected)
                self.assertFalse(any(q["sql"].lstrip().upper().startswith("UPDATE") for q in queries))
                role.delete()

    def test_upgrade_does_not_expand_custom_or_city_roles(self):
        cases = [
            ("support", "platform", "all", "support", False),
            ("custom", "platform", "all", "platform-admin", False),
            ("other-system", "platform", "all", "finance", True),
            ("city-scope", "platform", "city", "platform-admin", True),
            ("org-scope", "platform", "organization", "platform-admin", True),
            ("city-org", "city_agent", "all", "platform-admin", True),
            ("regional-org", "regional_agent", "all", "platform-admin", True),
        ]
        roles = []
        for label, kind, scope, code, is_system in cases:
            org = Organization.objects.create(name=label, code=label, organization_type=kind)
            roles.append(self.make_role(organization=org, code=code, is_system=is_system, data_scope=scope))
        roles.append(self.make_role(organization=None))
        for role in roles:
            before = list(role.permissions)
            self.apply_repair()
            role.refresh_from_db()
            self.assertEqual(role.permissions, before, role.organization_id)

    def test_city_scope_remains_denied_even_if_grants_were_manually_present(self):
        role = self.make_role(data_scope="city", permissions=["dashboard.view", *repair.PERMISSIONS])
        OrganizationMember.objects.create(user=self.user, organization=self.platform, role=role)
        self.apply_repair()
        self.assertTrue(CAMPAIGN_PERMISSIONS.isdisjoint(self.session_permissions()))
        self.assertEqual(self.client.get("/api/v1/admin/coupon-campaigns/").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/admin/coupon-campaigns/", {}, format="json").status_code, 403)

    def test_custom_view_only_role_cannot_publish(self):
        role = self.make_role(code="campaign-reader", is_system=False, permissions=["coupon_campaign.view"])
        OrganizationMember.objects.create(user=self.user, organization=self.platform, role=role)
        self.apply_repair()
        self.assertEqual(self.session_permissions(), {"coupon_campaign.view"})
        self.assertEqual(self.client.get("/api/v1/admin/coupon-campaigns/").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/admin/coupon-campaigns/", {}, format="json").status_code, 403)
