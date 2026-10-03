"""Isolated configuration tests. No real credentials, account creation or HTTP."""
from unittest.mock import patch

from django.db import DatabaseError
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from providers.huifu_user import OnboardingUnavailable, UserChannelConfig, channel_available
from providers.test_receiving_onboarding import CHANNEL_SETTINGS, CASH
from .models import AdminAuditLog, AdminRole, Organization, OrganizationMember, ReceivingWithdrawalSetting
from .receiving_settings import effective_cash_config


URL = "/api/v1/admin/operation-settings/receiving-withdrawal/"
PAYLOAD = {
    "cash_type": "T1", "out_fee_acct_type": "01", "fix_amt": "1.20", "fee_rate": None,
    "weekday_fix_amt": None, "weekday_fee_rate": None,
    "revision": 0, "confirmed": True, "reason": "已在隔离测试核对费用",
}


@override_settings(**CHANNEL_SETTINGS)
class ReceivingSettingsTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(phone="19900002221", password="offline-test")
        cls.user = User.objects.create_user(phone="19900002222", password="offline-test")

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def save(self, **changes):
        return self.client.put(URL, {**PAYLOAD, **changes}, format="json")

    def test_read_is_read_only_and_env_compatible(self):
        with patch("requests.sessions.Session.post") as http:
            response = self.client.get(URL)
            http.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        data = response.data["data"]
        self.assertEqual(data["source"], "environment")
        self.assertEqual(data["form"]["fix_amt"], "0.10")
        self.assertEqual(data["revision"], 0)
        self.assertTrue(data["onboarding_ready"])
        self.assertFalse(ReceivingWithdrawalSetting.objects.exists())
        self.assertFalse(AdminAuditLog.objects.exists())
        self.assertEqual(effective_cash_config(), CASH)

    def test_publish_audits_and_takes_precedence_without_network_or_env_changes(self):
        with patch("requests.sessions.Session.post") as http:
            response = self.save()
            http.assert_not_called()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual((data["source"], data["revision"]), ("admin", 1))
        self.assertEqual(effective_cash_config(), {**CASH, "fix_amt": "1.20"})
        self.assertEqual(UserChannelConfig.load(for_submission=True).settlement["fix_amt"], "1.20")
        audit = AdminAuditLog.objects.get()
        self.assertEqual(audit.action, "operations.receiving_withdrawal.update")
        self.assertEqual(audit.before["source"], "environment")
        self.assertEqual(audit.before["fix_amt"], "0.10")
        self.assertEqual(audit.after["reason"], PAYLOAD["reason"])
        self.assertEqual(audit.actor, self.admin)
        with self.settings(HUIFU_USER_CASH_CONFIG="invalid old env"):
            self.assertEqual(effective_cash_config()["fix_amt"], "1.20")

    def test_no_secrets_or_raw_ids_returned_or_audited(self):
        response = self.save()
        visible = str(response.data) + str(list(AdminAuditLog.objects.values("before", "after")))
        for name in ("HUIFU_RSA_PRIVATE_KEY", "HUIFU_RSA_PUBLIC_KEY", "HUIFU_USER_UPPER_ID", "HUIFU_MERCHANT_ID", "HUIFU_SYS_ID", "HUIFU_USER_NOTIFY_URL"):
            self.assertNotIn(CHANNEL_SETTINGS[name], visible)

    def test_missing_environment_explained_but_form_can_be_saved_with_switch_off(self):
        with self.settings(HUIFU_USER_ONBOARDING_ENABLED=False, HUIFU_USER_UPPER_ID="", HUIFU_USER_NOTIFY_URL="", HUIFU_USER_CASH_CONFIG=""):
            data = self.client.get(URL).data["data"]
            self.assertEqual(data["source"], "unconfigured")
            self.assertEqual(data["form"]["cash_type"], "")
            self.assertIsNone(data["form"]["fix_amt"])
            checks = {c["key"]: c["ok"] for c in data["checks"]}
            for name in ("onboarding_switch", "HUIFU_USER_UPPER_ID", "HUIFU_USER_NOTIFY_URL", "cash_config"):
                self.assertFalse(checks[name])
            response = self.save()
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.data["data"]["onboarding_ready"])
            self.assertFalse(channel_available())

    def test_rejects_invalid_or_unconfirmed_rules_without_writing(self):
        for changes in (
            {"confirmed": False}, {"confirmed": "true"}, {"reason": " "},
            {"cash_type": "D0"}, {"out_fee_acct_type": "99"},
            {"fix_amt": None, "fee_rate": None}, {"fix_amt": "-0.01"}, {"fix_amt": "0.001"},
            {"fix_amt": "1000.00"}, {"fee_rate": "100.01"}, {"fee_rate": "NaN"},
            {"weekday_fee_rate": "0.10"}, {"out_fee_flag": "2"},
            {"out_fee_huifu_id": "9000000000000003"}, {"HUIFU_USER_ONBOARDING_ENABLED": True},
        ):
            with self.subTest(changes=changes):
                response = self.save(**changes)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ReceivingWithdrawalSetting.objects.exists())
        self.assertFalse(AdminAuditLog.objects.exists())

    def test_missing_merchant_cannot_publish_guessed_fee_bearer(self):
        with self.settings(HUIFU_MERCHANT_ID=""):
            self.assertEqual(self.save().status_code, 400)
        self.assertFalse(ReceivingWithdrawalSetting.objects.exists())

    def test_malformed_request_and_invalid_env_have_safe_errors(self):
        for payload in ([], "invalid", {**PAYLOAD, "confirmed": 1}):
            self.assertEqual(self.client.put(URL, payload, format="json").status_code, 400)
        with self.settings(HUIFU_USER_CASH_CONFIG='{"private":"do-not-echo"}'):
            response = self.client.get(URL)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.data["data"]["onboarding_ready"])
            self.assertNotIn("do-not-echo", str(response.data))
            self.assertEqual(response.data["data"]["form"]["cash_type"], "")

    def test_d1_and_explicit_zero_preserve_null_vs_zero(self):
        response = self.save(cash_type="D1", fix_amt="0", fee_rate="0.05", weekday_fix_amt="0", weekday_fee_rate=None)
        self.assertEqual(response.status_code, 200, response.data)
        cash = effective_cash_config()
        self.assertEqual(cash["fix_amt"], "0.00")
        self.assertEqual(cash["weekday_fix_amt"], "0.00")
        self.assertEqual(cash["fee_rate"], "0.05")
        self.assertNotIn("weekday_fee_rate", cash)
        self.assertEqual(response.data["data"]["form"]["weekday_fee_rate"], None)

    def test_stale_revision_cannot_overwrite_published_rule(self):
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.save(fix_amt="2.20").status_code, 409)
        self.assertEqual(effective_cash_config()["fix_amt"], "1.20")
        self.assertEqual(AdminAuditLog.objects.count(), 1)
        self.assertEqual(self.save(revision=1, fix_amt="2.20").status_code, 200)
        self.assertEqual(AdminAuditLog.objects.count(), 2)
        self.assertEqual(ReceivingWithdrawalSetting.objects.get().revision, 2)

    def test_audit_failure_rolls_back_configuration(self):
        with patch("backoffice.receiving_setting_views.AdminAuditLog.objects.create", side_effect=RuntimeError("test audit failure")):
            with self.assertRaises(RuntimeError):
                self.save()
        self.assertFalse(ReceivingWithdrawalSetting.objects.exists())

    def test_invalid_published_config_or_db_error_never_falls_back(self):
        self.save()
        ReceivingWithdrawalSetting.objects.update(cash_type="D0")
        self.assertFalse(channel_available())
        with self.assertRaises(OnboardingUnavailable):
            effective_cash_config()
        self.assertFalse(self.client.get(URL).data["data"]["onboarding_ready"])
        with patch("backoffice.receiving_settings.ReceivingWithdrawalSetting.objects.filter", side_effect=DatabaseError("private connection detail")):
            with self.assertRaises(OnboardingUnavailable):
                effective_cash_config()
            response = self.client.get(URL)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private connection detail", str(response.data))

    def test_changed_merchant_requires_explicit_republication(self):
        self.save()
        with self.settings(HUIFU_MERCHANT_ID="9000000000000009"):
            self.assertFalse(channel_available())
            self.assertFalse(self.client.get(URL).data["data"]["onboarding_ready"])
            self.assertEqual(self.save(revision=1).status_code, 200)
            self.assertEqual(effective_cash_config()["out_fee_huifu_id"], "9000000000000009")

    def test_invalid_notify_url_is_reported_without_crashing(self):
        with self.settings(HUIFU_USER_NOTIFY_URL="https://[invalid"):
            self.assertFalse(channel_available())
            self.assertFalse(self.client.get(URL).data["data"]["onboarding_ready"])

    def test_permissions_and_platform_all_scope_are_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(URL).status_code, 401)
        self.assertEqual(self.save().status_code, 401)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.save().status_code, 403)
        org = Organization.objects.create(name="测试平台", code="test", organization_type="platform")
        role = AdminRole.objects.create(organization=org, name="运营", code="test", permissions=["operations.manage"], data_scope="city")
        OrganizationMember.objects.create(user=self.user, organization=org, role=role)
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.save().status_code, 403)
        role.data_scope = "all"
        role.permissions = ["provider.view"]
        role.save()
        self.assertEqual(self.client.get(URL).status_code, 403)
        role.permissions = ["operations.manage"]
        role.save()
        self.assertEqual(self.client.get(URL).status_code, 200)
        org.organization_type = "city_agent"
        org.save()
        role.permissions = ["*"]
        role.save()
        self.assertEqual(self.save().status_code, 403)
