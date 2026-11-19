from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import User, UserFunctionRestriction as Restriction
from accounts.views import auth_payload
from config.authentication import VersionedJWTAuthentication
from notifications.models import UserNotification
from . import tests as fixtures
from .models import AdminAuditLog


class UserFunctionManagementTests(APITestCase):
    setUpTestData = classmethod(fixtures.BackofficeProviderReviewTests.setUpTestData.__func__)

    def setUp(self):
        self.client.force_authenticate(self.admin_user)

    def change(self, functions, action="restrict", user=None, reason="用户投诉核查处理中"):
        return self.client.post(
            reverse("backoffice-user-function-action", args=((user or self.handan_user).public_id,)),
            {"action": action, "functions": functions, "reason": reason}, format="json",
        )

    def test_multiple_restrictions_preserve_account_login_and_provider_receiving_state(self):
        token = auth_payload(self.handan_user)["access"]
        version = self.handan_user.auth_version
        self.handan.is_accepting_orders = True
        self.handan.save(update_fields=("is_accepting_orders",))
        response = self.change(["provider_order", "review"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["function"] for item in response.data["data"]["function_restrictions"]},
            {"provider_order", "review"},
        )
        self.handan_user.refresh_from_db()
        self.handan.refresh_from_db()
        self.assertEqual(self.handan_user.account_status, User.AccountStatus.ACTIVE)
        self.assertEqual(self.handan_user.auth_version, version)
        self.assertTrue(self.handan.is_accepting_orders)
        authentication = VersionedJWTAuthentication()
        authenticated = authentication.get_user(authentication.get_validated_token(token))
        self.assertEqual(authenticated.pk, self.handan_user.pk)
        self.assertTrue(auth_payload(self.handan_user)["access"])
        audit = AdminAuditLog.objects.get(action="user.function.restrict")
        self.assertEqual(audit.after["functions"], ["provider_order", "review"])
        self.assertEqual(audit.after["reason"], "用户投诉核查处理中")
        self.assertEqual(UserNotification.objects.filter(recipient=self.handan_user).count(), 1)
        detail = self.client.get(reverse("backoffice-user-detail", args=(self.handan_user.public_id,)))
        self.assertEqual(len(detail.data["data"]["function_restrictions"]), 2)

    def test_restoring_one_function_preserves_other_and_keeps_audit(self):
        self.assertEqual(self.change(["provider_order", "review"]).status_code, 200)
        response = self.change(["provider_order"], "restore", reason="下单限制核实后解除")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["function"] for r in response.data["data"]["function_restrictions"]], ["review"])
        restored = Restriction.objects.get(user=self.handan_user, function="provider_order")
        self.assertFalse(restored.is_active)
        self.assertEqual(restored.restored_by, self.admin_user)
        self.assertIsNotNone(restored.restored_at)
        self.assertEqual(AdminAuditLog.objects.filter(action="user.function.restore").count(), 1)
        self.assertEqual(self.change(["provider_order"]).status_code, 200)
        restored.refresh_from_db()
        self.assertTrue(restored.is_active)
        self.assertIsNone(restored.restored_at)

    def test_restoring_account_does_not_clear_function_restrictions(self):
        self.change(["review"])
        account_url = reverse("backoffice-user-account-action", args=(self.handan_user.public_id,))
        for action in ("suspend", "restore"):
            self.assertEqual(self.client.post(account_url, {"action": action, "reason": "安全复核处理"}).status_code, 200)
        self.assertTrue(Restriction.objects.filter(user=self.handan_user, function="review", is_active=True).exists())

    def test_old_whole_account_restrict_is_retired_and_historical_state_preserved(self):
        url = reverse("backoffice-user-account-action", args=(self.handan_user.public_id,))
        self.assertEqual(self.client.post(url, {"action": "restrict", "reason": "旧客户端操作"}).status_code, 400)
        User.objects.filter(pk=self.handan_user.pk).update(account_status=User.AccountStatus.RESTRICTED)
        self.assertEqual(self.change(["review"]).status_code, 200)
        self.handan_user.refresh_from_db()
        self.assertEqual(self.handan_user.account_status, User.AccountStatus.RESTRICTED)

    def test_permissions_city_scope_and_protected_accounts(self):
        self.assertEqual(self.change(["review"], user=self.beijing_user).status_code, 404)
        self.assertEqual(self.change(["review"], user=self.platform_admin).status_code, 404)
        self.assertEqual(self.change(["review"], user=self.admin_user).status_code, 404)
        self.role.permissions = ["user.view"]
        self.role.save(update_fields=("permissions",))
        self.assertEqual(self.change(["review"]).status_code, 403)
        self.assertFalse(Restriction.objects.exists())

    def test_invalid_or_empty_changes_have_no_side_effects(self):
        for functions in ([], ["unknown"], ["review", "review"]):
            self.assertEqual(self.change(functions).status_code, 400)
        self.assertEqual(self.change(["review"], reason=" ").status_code, 400)
        self.assertFalse(Restriction.objects.exists())
        self.assertFalse(AdminAuditLog.objects.filter(action__startswith="user.function.").exists())
        self.assertFalse(UserNotification.objects.filter(recipient=self.handan_user).exists())
