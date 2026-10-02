"""Offline contract/state tests. Synthetic data; every outbound request is mocked."""
import hashlib
import hmac
import json
from unittest.mock import patch

from Crypto.PublicKey import RSA
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from .huifu_user import (
    ChannelUncertain, OnboardingUnavailable, UserChannelConfig, business_payload,
    registration_payload, settlement_config,
)
from .models import ProviderProfile, ProviderReceivingAccount, ProviderReceivingAttempt, ProviderReceivingNotification
from .receiving_accounts import CONSENT_VERSION, decrypt_details
from .receiving_onboarding import ONBOARDING_CONSENT_VERSION
from . import test_receiving_accounts as fixtures


URL = fixtures.URL
CONSENT = {"consent_accepted": True, "consent_version": ONBOARDING_CONSENT_VERSION}
# Generated ephemeral keys; no production credentials or real customer records.
KEY = RSA.generate(2048)
SETTLEMENT = {"settle_cycle": "T1", "settle_pattern": "P0", "settle_batch_no": "1000",
              "workday_fixed_ratio": "0.00", "workday_constant_amt": "0.00", "out_settle_flag": "2"}
CHANNEL_SETTINGS = dict(
    PROVIDER_RECEIVING_ACCOUNT_COLLECTION_ENABLED=True,
    PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY=fixtures.TEST_KEY,
    HUIFU_ENV="prod", HUIFU_USER_ONBOARDING_ENABLED=True,
    HUIFU_SYS_ID="9000000000000001", HUIFU_PRODUCT_ID="OFFLINE_TEST",
    HUIFU_USER_UPPER_ID="9000000000000002",
    HUIFU_RSA_PRIVATE_KEY=KEY.export_key(pkcs=8).decode(),
    HUIFU_RSA_PUBLIC_KEY=KEY.public_key().export_key().decode(),
    HUIFU_USER_NOTIFY_URL="https://example.invalid/api/v1/providers/receiving-account/huifu-notify/",
    HUIFU_USER_SKILL_SOURCE="hfps/1.3.5;hfms/1.0.4",
    HUIFU_USER_SETTLEMENT_CONFIG=json.dumps(SETTLEMENT),
)
USER_ID = "9000000000000003"
SUCCESS = {"resp_code": "00000000", "huifu_id": USER_ID}
ACCEPTED = {**SUCCESS, "apply_no": "OFFLINE1", "resp_business": '[{"type":"1","code":"S"},{"type":"3","code":"S"}]'}


@override_settings(**CHANNEL_SETTINGS)
class ReceivingOnboardingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(phone="19900000001", password="offline-test-only")
        self.provider = ProviderProfile.objects.create(
            user=self.user, status="approved", identity_status="verified",
            identity_real_name="测试达人", application_real_name="测试达人",
            identity_number_digest=hmac.new(settings.SECRET_KEY.encode(), fixtures.synthetic_id().encode(), hashlib.sha256).hexdigest(),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.materials = {
            "id_number": fixtures.synthetic_id(), "cert_begin_date": "2020-01-01",
            "cert_end_date": "2040-01-01", "cert_long_term": False,
            "mobile": "19900000001", "bank_card_number": "6222000000000000",
            "bank_name": "测试银行", "bank_province": "河北省", "bank_city": "邯郸市",
            "bank_province_code": "130000", "bank_city_code": "130400",
            "consent_accepted": True, "consent_version": CONSENT_VERSION,
        }
        self.assertEqual(self.client.put(URL, self.materials, format="json").status_code, 200)

    @property
    def account(self):
        return ProviderReceivingAccount.objects.get(provider=self.provider)

    def submit(self, **data):
        return self.client.post(URL + "submit/", {**CONSENT, **data}, format="json")

    def open(self):
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=[SUCCESS, ACCEPTED]) as gateway:
            response = self.submit()
        self.assertEqual(response.status_code, 200, response.data)
        return gateway

    def query_response(self, **changes):
        details = decrypt_details(self.account)
        return {
            "resp_code": "00000000",
            "indv_base_info": json.dumps({"name": details["real_name"], "cert_type": "00", "cert_no": details["id_number"]}),
            "card_info": json.dumps({"card_type": "1", "card_name": details["real_name"], "card_no": details["bank_card_number"], "prov_id": "130000", "area_id": "130400"}),
            "settle_config_list": json.dumps([{**SETTLEMENT, "settle_status": "1"}]),
            **changes,
        }

    def notify(self, state="Y", **changes):
        from dg_sdk.core.rsa_utils import rsa_sign
        attempt = self.account.attempts.get(kind="configure")
        data = {"notify_type": "A", "req_seq_id": attempt.req_seq_id, "req_date": attempt.req_date,
                "huifu_id": USER_ID, "sub_resp_code": "00000000", "sub_resp_desc": "审核/测试",
                "audit_info": json.dumps({"apply_no": "OFFLINE1", "audit_status": state, "resp_business": ACCEPTED["resp_business"]}), **changes}
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        valid, signature = rsa_sign(CHANNEL_SETTINGS["HUIFU_RSA_PRIVATE_KEY"], raw)
        self.assertTrue(valid)
        # Anonymous channel callback; signature is the authority, not the user's session.
        return APIClient().post("/api/v1/providers/receiving-account/huifu-notify/", {"data": raw, "sign": signature}, format="json")

    def test_disabled_misconfigured_and_mertest_never_send(self):
        for config in ({"HUIFU_USER_ONBOARDING_ENABLED": False}, {"HUIFU_USER_SETTLEMENT_CONFIG": "{}"},
                       {"HUIFU_ENV": "mertest"}, {"HUIFU_RSA_PRIVATE_KEY": "invalid"}):
            cache.clear()
            with self.settings(**config), patch("providers.receiving_onboarding.HuifuUserGateway.call") as call:
                self.assertEqual(self.submit().status_code, 503)
                call.assert_not_called()
        self.assertEqual(ProviderReceivingAttempt.objects.count(), 0)

    def test_unsupported_sdk_fails_before_recording_or_sending(self):
        with patch("dg_sdk.DGClient.__version__", "unvalidated"), patch("dg_sdk.V2UserBasicdataIndvRequest.post") as call:
            self.assertEqual(self.submit().status_code, 503)
            call.assert_not_called()
            self.assertFalse(self.client.get(URL).data["data"]["onboarding_enabled"])
        self.assertEqual(ProviderReceivingAttempt.objects.count(), 0)

    def test_requires_separate_explicit_consent(self):
        with patch("providers.receiving_onboarding.HuifuUserGateway.call") as call:
            for data in ({"consent_accepted": False}, {"consent_version": CONSENT_VERSION}, {"consent_accepted": "true"}):
                self.assertEqual(self.submit(**data).status_code, 400)
            call.assert_not_called()

    def test_real_sdk_signed_register_configure_and_query_chain(self):
        from .test_huifu_user_transport import signed_http_response
        # No gateway mock: execute all official request classes and response checks.
        responses = [signed_http_response(SUCCESS, key=KEY), signed_http_response(ACCEPTED, key=KEY),
                     signed_http_response(self.query_response(), key=KEY)]
        with patch("requests.sessions.Session.post", side_effect=responses) as http:
            self.assertEqual(self.submit().status_code, 200)
            self.assertEqual(self.account.channel_status, "pending")
            self.assertEqual(self.client.post(URL + "refresh/").status_code, 200)
            self.assertEqual(self.account.channel_status, "active")
        self.assertEqual(http.call_count, 3)
        self.assertEqual(self.account.attempts.count(), 3)

    def test_real_sdk_missing_signature_never_creates_or_configures_again(self):
        from .test_huifu_user_transport import http_response
        with patch("requests.sessions.Session.post", return_value=http_response(SUCCESS)) as http:
            self.assertEqual(self.submit().status_code, 200)
            self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(http.call_count, 1)
        self.assertIsNone(self.account.user_huifu_id)
        self.assertEqual(self.account.channel_status, "attention")

    def test_real_sdk_bank_response_failure_preserves_registered_user(self):
        from .test_huifu_user_transport import http_response, signed_http_response
        with patch("requests.sessions.Session.post", side_effect=[signed_http_response(SUCCESS, key=KEY), http_response(ACCEPTED)]):
            self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.account.user_huifu_id, USER_ID)
        self.assertEqual(self.account.channel_status, "attention")

    def test_unverified_and_other_user_cannot_submit_or_read(self):
        self.provider.identity_status = "pending"
        self.provider.save()
        self.assertEqual(self.submit().status_code, 403)
        self.client.force_authenticate(None)
        for path in ("submit/", "refresh/"):
            self.assertEqual(self.client.post(URL + path, CONSENT, format="json").status_code, 401)
        other = User.objects.create_user(phone="19900000002", password="offline-test-only")
        ProviderProfile.objects.create(user=other, status="approved")
        self.client.force_authenticate(other)
        self.assertFalse(self.client.get(URL).data["data"]["materials_saved"])
        self.assertEqual(self.submit().status_code, 400)

    def test_full_registration_then_configuration_is_not_payout_success(self):
        calls = self.open().call_args_list
        account = self.account
        self.assertEqual(account.channel_status, "pending")
        self.assertEqual(account.user_huifu_id, USER_ID)
        self.assertEqual([c.args[0] for c in calls], ["register", "configure"])
        registration, business = calls[0].args[1], calls[1].args[1]
        self.assertNotIn("huifu_id", registration)
        self.assertNotIn("upper_huifu_id", registration)
        self.assertEqual(registration["cert_validity_type"], "0")
        self.assertEqual(registration["cert_end_date"], "20400101")
        self.assertEqual(business["upper_huifu_id"], CHANNEL_SETTINGS["HUIFU_USER_UPPER_ID"])
        self.assertEqual(json.loads(business["card_info"])["card_type"], "1")
        self.assertEqual(json.loads(business["settle_config_list"]), [SETTLEMENT])
        self.assertNotEqual(registration["req_seq_id"], business["req_seq_id"])
        self.assertEqual(account.onboarding_consent_version, ONBOARDING_CONSENT_VERSION)
        result = self.client.get(URL).data["data"]
        self.assertFalse(result["can_edit"])
        self.assertFalse(result["can_clear"])
        self.assertNotIn(USER_ID, str(result))
        for field in ("id_number", "mobile", "bank_card_number"):
            self.assertNotIn(self.materials[field], str(result))

    def test_repeat_submit_does_not_create_another_account(self):
        self.open()
        with patch("providers.receiving_onboarding.HuifuUserGateway.call") as call:
            self.assertEqual(self.submit().status_code, 200)
            call.assert_not_called()
        self.assertEqual(self.account.attempts.count(), 2)

    def test_timeout_is_unknown_and_never_automatically_recreated(self):
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=ChannelUncertain()):
            self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.account.channel_status, "attention")
        with patch("providers.receiving_onboarding.HuifuUserGateway.call") as call:
            self.submit()
            call.assert_not_called()
        self.assertFalse(self.client.get(URL).data["data"]["can_edit"])

    def test_successful_registration_id_survives_configuration_failure(self):
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=[SUCCESS, ChannelUncertain()]):
            self.submit()
        self.assertEqual(self.account.user_huifu_id, USER_ID)
        self.assertEqual(self.account.channel_status, "attention")
        self.assertEqual(self.client.put(URL, self.materials, format="json").status_code, 400)
        self.assertEqual(self.client.delete(URL).status_code, 400)

    def test_pending_request_cannot_be_queried_concurrently(self):
        account = self.account
        account.channel_status = "registering"
        account.channel_scope = UserChannelConfig.load().scope
        account.onboarding_consented_at = timezone.now()
        account.save()
        ProviderReceivingAttempt.objects.create(account=account, kind="register", req_seq_id="a" * 32, req_date="20261002")
        with patch("providers.receiving_onboarding.HuifuUserGateway.call") as call:
            self.assertEqual(self.client.post(URL + "refresh/").status_code, 200)
            call.assert_not_called()

    def test_foreign_scope_cannot_query_or_recreate(self):
        self.open()
        with self.settings(HUIFU_USER_UPPER_ID="9000000000000009"), patch("providers.receiving_onboarding.HuifuUserGateway.call") as call:
            self.assertEqual(self.submit().status_code, 400)
            self.assertEqual(self.client.post(URL + "refresh/").status_code, 400)
            call.assert_not_called()

    def test_recover_unknown_matches_identity_before_binding_id(self):
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=ChannelUncertain()):
            self.submit()
        recovery = {"resp_code": "00000000", "user_list_info_list": json.dumps([{"cust_type": "2", "name": "测试达人", "upper_huifu_id": CHANNEL_SETTINGS["HUIFU_USER_UPPER_ID"], "huifu_id": USER_ID}])}
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=[recovery, self.query_response()]) as call:
            self.assertEqual(self.client.post(URL + "refresh/").status_code, 200)
            self.assertEqual([c.args[0] for c in call.call_args_list], ["recover", "query"])
        self.assertEqual(self.account.channel_status, "registered")
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", return_value=ACCEPTED) as call:
            self.submit()
            self.assertEqual(call.call_args.args[0], "configure")

    def test_recovery_empty_result_never_allows_recreation(self):
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=ChannelUncertain()):
            self.submit()
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", return_value={"resp_code": "00000000", "user_list_info_list": "[]"}):
            self.client.post(URL + "refresh/")
        self.assertEqual(self.account.channel_status, "attention")
        self.assertIsNone(self.account.user_huifu_id)

    def test_active_requires_matching_identity_card_and_enabled_settlement(self):
        self.open()
        for changes in ({"indv_base_info": "{}"}, {"card_info": "{}"}, {"settle_config_list": '[{"settle_cycle":"T1","settle_status":"0"}]'}, {"settle_config_list": [{"settle_cycle": "T1", "settle_status": "1"}]}):
            with patch("providers.receiving_onboarding.HuifuUserGateway.call", return_value=self.query_response(**changes)):
                self.client.post(URL + "refresh/")
            self.assertNotEqual(self.account.channel_status, "active")
        cache.clear()
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", return_value=self.query_response()):
            self.client.post(URL + "refresh/")
        self.assertEqual(self.account.channel_status, "active")
        self.assertIsNotNone(self.account.channel_checked_at)

    def test_query_rejects_mismatched_user_even_when_materials_match(self):
        self.open()
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", return_value=self.query_response(huifu_id="9000000000000009")):
            self.assertEqual(self.client.post(URL + "refresh/").status_code, 200)
        self.assertEqual(self.account.channel_status, "attention")
        self.assertEqual(self.account.user_huifu_id, USER_ID)
        self.assertEqual(self.account.attempts.get(kind="query").status, "unknown")
        self.assertIsNone(self.account.channel_checked_at)

    def test_recovery_does_not_bind_id_from_mismatched_detail_response(self):
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=ChannelUncertain()):
            self.submit()
        recovery = {"resp_code": "00000000", "user_list_info_list": json.dumps([{"cust_type": "2", "name": "测试达人", "upper_huifu_id": CHANNEL_SETTINGS["HUIFU_USER_UPPER_ID"], "huifu_id": USER_ID}])}
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=[recovery, self.query_response(huifu_id="9000000000000009")]):
            self.assertEqual(self.client.post(URL + "refresh/").status_code, 200)
        self.assertIsNone(self.account.user_huifu_id)
        self.assertEqual(self.account.channel_status, "attention")

    def test_signed_callback_progression_deduplication_and_query_confirmation(self):
        self.open()
        self.assertEqual(self.notify("P").status_code, 200)
        response = self.notify("Y")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.content.decode(), "RECV_ORD_ID_" + self.account.attempts.get(kind="configure").req_seq_id)
        self.assertEqual(self.account.channel_status, "pending")
        self.notify("Y")
        self.notify("P")
        self.assertEqual(ProviderReceivingNotification.objects.count(), 2)
        self.assertEqual(self.account.audit_status, "Y")
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", return_value=self.query_response()):
            self.client.post(URL + "refresh/")
        self.assertEqual(self.account.channel_status, "active")

    def test_invalid_signature_or_wrong_correlation_does_not_change_account(self):
        self.open()
        path = "/api/v1/providers/receiving-account/huifu-notify/"
        response = APIClient().post(path, {"data": "{}", "sign": "invalid"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.notify(huifu_id="9000000000000008").status_code, 400)
        self.assertEqual(self.notify(req_seq_id="f" * 32).status_code, 400)
        self.assertEqual(self.account.audit_status, "")
        self.assertEqual(ProviderReceivingNotification.objects.count(), 0)

    def test_rejected_audit_is_not_overridden_by_stale_pending_or_query(self):
        self.open()
        self.notify("N")
        self.notify("P")
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", return_value=self.query_response()):
            self.client.post(URL + "refresh/")
        self.assertEqual(self.account.audit_status, "N")
        self.assertEqual(self.account.channel_status, "attention")

    def test_callback_before_sync_response_keeps_terminal_audit(self):
        def call(kind, payload):
            if kind == "register":
                return SUCCESS
            self.assertEqual(self.notify("N").status_code, 200)
            return ACCEPTED
        with patch("providers.receiving_onboarding.HuifuUserGateway.call", side_effect=call):
            self.submit()
        self.assertEqual(self.account.audit_status, "N")
        self.assertEqual(self.account.channel_status, "attention")
        self.assertEqual(self.account.attempts.get(kind="configure").status, "rejected")

    def test_channel_application_blocks_account_closure(self):
        from accounts.account_closure import request_account_closure
        from rest_framework.exceptions import ValidationError
        self.open()
        with self.assertRaises(ValidationError):
            request_account_closure(self.user, "offline-test-only")

    def test_regions_validate_relationship_and_return_official_codes(self):
        self.assertEqual(self.client.put(URL, {**self.materials, "bank_city_code": "110100"}, format="json").status_code, 400)
        data = self.client.get(URL + "regions/").data["data"]
        province = next(p for p in data if p["code"] == "130000")
        self.assertIn({"name": "邯郸市", "code": "130400"}, province["cities"])
        province = next(p for p in data if p["code"] == "620000")
        self.assertIn({"name": "嘉峪关市", "code": "620200"}, province["cities"])

    def test_long_term_cert_omits_end_date_in_both_requests(self):
        details = decrypt_details(self.account)
        details.update(cert_long_term=True, cert_end_date=None)
        attempt = ProviderReceivingAttempt(req_date="20261002", req_seq_id="a" * 32, settlement_config=SETTLEMENT)
        self.assertNotIn("cert_end_date", registration_payload(attempt, details))
        card = json.loads(business_payload(attempt, self.account, details, UserChannelConfig.load())["card_info"])
        self.assertEqual(card["cert_validity_type"], "1")
        self.assertNotIn("cert_end_date", card)

    def test_settlement_has_no_implicit_business_defaults(self):
        self.assertEqual(settlement_config(SETTLEMENT), SETTLEMENT)
        for raw in ({}, {**SETTLEMENT, "settle_cycle": "D1"}, {**SETTLEMENT, "out_settle_flag": "1"},
                    {**SETTLEMENT, "workday_fixed_ratio": "101.00"}, {**SETTLEMENT, "settle_batch_no": "guess"}):
            with self.assertRaises(OnboardingUnavailable):
                settlement_config(raw)
