"""Offline SMS gateway and auth-entry regression. All provider calls are mocked."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
import logging
from threading import Event
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.retry import NoopRetryer
from tencentcloud.sms.v20210111.models import SendSmsResponse

from .models import User
from .services import (
    SMS_PURPOSES, _code_attempt_key, _code_key, _cooldown_key, _sms_phone_daily_key,
    send_sms_code, verify_sms_code,
)
from .tencent_sms import TEMPLATE_SETTINGS, SmsConfig, SmsDeliveryUnavailable
from .wechat_login import WechatIdentity, _issue_ticket


SMS_SETTINGS = {
    "DEBUG": False,
    "TENCENT_SMS_ENABLED": True,
    "TENCENT_SMS_SECRET_ID": "unit-secret-id",
    "TENCENT_SMS_SECRET_KEY": "unit-secret-key",
    "TENCENT_SMS_SDK_APP_ID": "1400000000",
    "TENCENT_SMS_SIGN_NAME": "测试签名",
    "TENCENT_SMS_TEMPLATE_ID": "1234",
    "TENCENT_SMS_TEMPLATE_ID_REGISTER": "",
    "TENCENT_SMS_TEMPLATE_ID_LOGIN": "",
    "TENCENT_SMS_TEMPLATE_ID_RESET_PASSWORD": "",
    "TENCENT_SMS_TEMPLATE_ID_INITIAL_PASSWORD": "",
    "TENCENT_SMS_TEMPLATE_ID_WECHAT_BIND": "",
    "TENCENT_SMS_TEMPLATE_ID_CHANGE_PHONE": "",
    "TENCENT_SMS_TEMPLATE_PARAMS": ("code",),
    "TENCENT_SMS_REGION": "ap-guangzhou",
    "TENCENT_SMS_TIMEOUT_SECONDS": 10,
    "SMS_CODE_TTL_SECONDS": 300,
    "SMS_CODE_RESEND_SECONDS": 60,
    "SMS_CODE_MAX_ATTEMPTS": 5,
    "SMS_PHONE_DAILY_LIMIT": 10,
}


def accepted_response(phone="13800000801", **overrides):
    status = {
        "Code": "Ok", "PhoneNumber": f"+86{phone}", "SerialNo": "unit-serial-01",
        "Message": "send success", "Fee": 1, **overrides,
    }
    response = SendSmsResponse()
    response.from_json_string(json.dumps({
        "SendStatusSet": [status], "RequestId": "unit-request-01",
    }))
    return response


@override_settings(**SMS_SETTINGS)
class TencentSmsTests(SimpleTestCase):
    phone = "13800000801"

    def setUp(self):
        cache.clear()
        client_patch = patch("accounts.tencent_sms.SmsClient")
        self.client_class = client_patch.start()
        self.addCleanup(client_patch.stop)
        self.client = self.client_class.return_value
        self.client.SendSms.return_value = accepted_response()
        random_patch = patch("accounts.services.secrets.randbelow", return_value=9876)
        random_patch.start()
        self.addCleanup(random_patch.stop)

    def send(self, purpose="register"):
        return send_sms_code(phone=self.phone, purpose=purpose)

    def test_request_is_signed_by_sdk_and_has_server_owned_fields(self):
        result = self.send()
        self.assertIsNone(result.debug_code)
        self.assertEqual((result.expires_in, result.retry_after), (300, 60))
        credential, region, profile = self.client_class.call_args.args
        self.assertEqual(region, "ap-guangzhou")
        self.assertEqual(credential.secretId, "unit-secret-id")
        self.assertEqual(profile.signMethod, "TC3-HMAC-SHA256")
        self.assertIsInstance(profile.retryer, NoopRetryer)
        self.assertTrue(profile.disable_region_breaker)
        self.assertEqual(profile.httpProfile.scheme, "https")
        self.assertIsNot(profile.httpProfile.certification, False)
        self.assertEqual(profile.httpProfile.reqTimeout, 10)
        self.assertEqual(profile.httpProfile.endpoint, "sms.tencentcloudapi.com")
        request = self.client.SendSms.call_args.args[0]
        self.assertEqual(request.PhoneNumberSet, [f"+86{self.phone}"])
        self.assertEqual(request.SmsSdkAppId, "1400000000")
        self.assertEqual(request.SignName, "测试签名")
        self.assertEqual(request.TemplateId, "1234")
        self.assertEqual(request.TemplateParamSet, ["009876"])
        self.assertEqual(cache.get(_code_key(self.phone, "register")), "009876")
        self.assertTrue(logging.getLogger("tencentcloud_sdk_common").disabled)
        self.assertNotIn("unit-secret", repr(SmsConfig.load()))

    def test_actual_sdk_serializes_request_and_reads_send_status(self):
        # Exercise the installed SDK, not just the client-constructor mock.
        from tencentcloud.sms.v20210111.sms_client import SmsClient as RealSmsClient

        self.client_class.side_effect = RealSmsClient
        with patch.object(RealSmsClient, "call", return_value=json.dumps({"Response": {
            "SendStatusSet": [{
                "Code": "Ok", "PhoneNumber": f"+86{self.phone}", "SerialNo": "unit-serial-01",
            }],
            "RequestId": "unit-request-01",
        }})) as call:
            self.send()
        self.assertEqual(call.call_args.args[0], "SendSms")
        self.assertEqual(call.call_args.args[1]["TemplateParamSet"], ["009876"])

    @override_settings(TENCENT_SMS_TEMPLATE_PARAMS=("code", "minutes"), SMS_CODE_TTL_SECONDS=600)
    def test_two_variable_template_matches_code_then_minutes(self):
        self.assertEqual(self.send().expires_in, 600)
        self.assertEqual(self.client.SendSms.call_args.args[0].TemplateParamSet, ["009876", "10"])

    def test_scene_overrides_take_priority_for_every_purpose(self):
        self.assertEqual(set(TEMPLATE_SETTINGS), SMS_PURPOSES)
        for index, (purpose, setting) in enumerate(TEMPLATE_SETTINGS.items(), start=1):
            cache.clear()
            template_id = str(9000 + index)
            with self.subTest(purpose=purpose), override_settings(**{setting: template_id}):
                self.send(purpose)
                self.assertEqual(self.client.SendSms.call_args.args[0].TemplateId, template_id)

    def test_blank_scene_templates_use_default_for_every_purpose(self):
        for purpose, setting in TEMPLATE_SETTINGS.items():
            cache.clear()
            with self.subTest(purpose=purpose), override_settings(**{setting: "  "}):
                self.send(purpose)
                self.assertEqual(self.client.SendSms.call_args.args[0].TemplateId, "1234")

    @override_settings(TENCENT_SMS_TEMPLATE_ID="", TENCENT_SMS_TEMPLATE_ID_REGISTER="9001")
    def test_default_is_only_required_for_scenes_without_an_override(self):
        self.send()
        self.assertEqual(self.client.SendSms.call_args.args[0].TemplateId, "9001")
        with self.assertRaises(SmsDeliveryUnavailable):
            self.send("login")
        self.client.SendSms.assert_called_once()

    @override_settings(TENCENT_SMS_TEMPLATE_ID_REGISTER="not-an-id")
    def test_invalid_explicit_template_is_not_silently_replaced(self):
        with self.assertRaises(SmsDeliveryUnavailable):
            self.send()
        self.client_class.assert_not_called()
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))

    @override_settings(TENCENT_SMS_TEMPLATE_ID_REGISTER="9001")
    def test_provider_rejection_does_not_trigger_a_second_send_using_default(self):
        self.client.SendSms.return_value = accepted_response(
            Code="FailedOperation.TemplateIncorrectOrUnapproved",
        )
        with self.assertRaises(SmsDeliveryUnavailable):
            self.send()
        self.client.SendSms.assert_called_once()
        self.assertEqual(self.client.SendSms.call_args.args[0].TemplateId, "9001")
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))

    @override_settings(DEBUG=True, SMS_DEVELOPMENT_CODE="123456")
    def test_enabled_gateway_never_returns_debug_code_even_in_debug_mode(self):
        self.assertIsNone(self.send().debug_code)
        self.assertEqual(cache.get(_code_key(self.phone, "register")), "009876")
        self.client.SendSms.assert_called_once()

    @override_settings(TENCENT_SMS_ENABLED=False)
    def test_disabled_production_is_fail_closed(self):
        with self.assertRaises(SmsDeliveryUnavailable):
            self.send()
        self.client_class.assert_not_called()
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))

    @override_settings(TENCENT_SMS_ENABLED=False, DEBUG=True, SMS_DEVELOPMENT_CODE="123456")
    def test_explicitly_disabled_local_mode_preserves_existing_mock(self):
        self.assertEqual(self.send().debug_code, "123456")
        self.client_class.assert_not_called()

    def test_incomplete_or_unsafe_config_never_sends_or_activates(self):
        for overrides in (
            {"TENCENT_SMS_SECRET_KEY": ""},
            {"TENCENT_SMS_SDK_APP_ID": ""},
            {"TENCENT_SMS_SDK_APP_ID": "wx-app"},
            {"TENCENT_SMS_TEMPLATE_ID": ""},
            {"TENCENT_SMS_SIGN_NAME": ""},
            {"TENCENT_SMS_TEMPLATE_PARAMS": ("unknown",)},
            {"TENCENT_SMS_TEMPLATE_PARAMS": ("minutes", "code")},
            {"TENCENT_SMS_TEMPLATE_PARAMS": ("code", "minutes"), "SMS_CODE_TTL_SECONDS": 61},
            {"SMS_CODE_TTL_SECONDS": 0},
            {"SMS_PHONE_DAILY_LIMIT": 0},
            {"SMS_CODE_MAX_ATTEMPTS": 0},
        ):
            with self.subTest(overrides=overrides), override_settings(**overrides):
                with self.assertRaises(SmsDeliveryUnavailable):
                    self.send()
        self.client_class.assert_not_called()
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))
        self.assertIsNone(cache.get(_cooldown_key(self.phone)))

    @override_settings(
        TENCENT_SMS_SECRET_ID="", TENCENT_SMS_SECRET_KEY="",
        TENCENT_CLOUD_SECRET_ID="shared-id", TENCENT_CLOUD_SECRET_KEY="shared-key",
    )
    def test_credentials_fall_back_only_as_a_complete_pair(self):
        self.assertEqual(SmsConfig.load().secret_id, "shared-id")
        with override_settings(TENCENT_SMS_SECRET_ID="partial-only"):
            with self.assertRaises(SmsDeliveryUnavailable):
                SmsConfig.load()

    def test_all_purposes_use_gateway_but_codes_are_isolated_and_one_use(self):
        for purpose in sorted(SMS_PURPOSES):
            with self.subTest(purpose=purpose):
                cache.clear()
                self.send(purpose)
                other = "login" if purpose != "login" else "register"
                with self.assertRaises(ValidationError):
                    verify_sms_code(phone=self.phone, purpose=other, code="009876")
                verify_sms_code(phone=self.phone, purpose=purpose, code="009876")
                with self.assertRaises(ValidationError):
                    verify_sms_code(phone=self.phone, purpose=purpose, code="009876")
        self.assertEqual(self.client.SendSms.call_count, len(SMS_PURPOSES))

    def test_failed_or_mismatched_response_never_activates_new_code(self):
        cases = [
            accepted_response(Code="FailedOperation.TemplateIncorrectOrUnapproved"),
            accepted_response(phone="13800000802"),
            accepted_response(SerialNo=""),
            accepted_response(Code="ok"),
        ]
        empty = accepted_response()
        empty.SendStatusSet = []
        cases.append(empty)
        multiple = accepted_response()
        multiple.SendStatusSet *= 2
        cases.append(multiple)
        missing_id = accepted_response()
        missing_id.RequestId = ""
        cases.append(missing_id)
        for response in cases:
            with self.subTest(response=response.to_json_string()):
                cache.clear()
                self.client.SendSms.return_value = response
                with self.assertRaises(SmsDeliveryUnavailable):
                    self.send()
                self.assertIsNone(cache.get(_code_key(self.phone, "register")))
                self.assertIsNotNone(cache.get(_cooldown_key(self.phone)))

    def test_timeout_and_sdk_errors_do_not_retry_or_log_sensitive_text(self):
        sensitive = f"{self.phone} 009876 unit-secret-key"
        for error in (
            TimeoutError(sensitive),
            TencentCloudSDKException("AuthFailure.SecretIdNotFound", sensitive, "unit-request-02"),
        ):
            with self.subTest(error_type=type(error).__name__):
                cache.clear()
                self.client.SendSms.reset_mock()
                self.client.SendSms.side_effect = error
                with self.assertLogs("accounts.tencent_sms", level="WARNING") as logs:
                    with self.assertRaises(SmsDeliveryUnavailable) as caught:
                        self.send()
                self.assertNotIn(sensitive, str(caught.exception))
                for value in (self.phone, "009876", "unit-secret-key"):
                    self.assertNotIn(value, " ".join(logs.output))
                self.client.SendSms.assert_called_once()
                self.assertIsNone(cache.get(_code_key(self.phone, "register")))
                with self.assertRaises(ValidationError):
                    self.send("login")
                self.client.SendSms.assert_called_once()

    def test_failure_keeps_previous_code_and_attempt_count(self):
        cache.set(_code_key(self.phone, "register"), "654321", 300)
        cache.set(_code_attempt_key(self.phone, "register"), 2, 300)
        self.client.SendSms.side_effect = TimeoutError()
        with self.assertRaises(SmsDeliveryUnavailable):
            self.send()
        self.assertEqual(cache.get(_code_key(self.phone, "register")), "654321")
        self.assertEqual(cache.get(_code_attempt_key(self.phone, "register")), 2)

    @override_settings(SMS_CODE_RESEND_SECONDS=0)
    def test_real_gateway_always_has_minimum_sixty_second_cooldown(self):
        self.assertEqual(self.send().retry_after, 60)
        with self.assertRaises(ValidationError):
            self.send("login")
        self.client.SendSms.assert_called_once()

    @override_settings(SMS_PHONE_DAILY_LIMIT=1)
    def test_daily_quota_counts_failed_attempts_across_purposes(self):
        self.client.SendSms.side_effect = TimeoutError()
        with self.assertRaises(SmsDeliveryUnavailable):
            self.send()
        cache.delete(_cooldown_key(self.phone))  # Simulate cooldown expiry, not day expiry.
        with self.assertRaisesMessage(ValidationError, "今日获取验证码次数已达上限"):
            self.send("login")
        self.assertEqual(cache.get(_sms_phone_daily_key(self.phone)), 2)
        self.client.SendSms.assert_called_once()

    def test_expired_or_exhausted_code_cannot_be_used(self):
        self.send()
        cache.delete(_code_key(self.phone, "register"))
        with self.assertRaises(ValidationError):
            verify_sms_code(phone=self.phone, purpose="register", code="009876")
        cache.clear()
        self.send()
        for _ in range(5):
            with self.assertRaises(ValidationError):
                verify_sms_code(phone=self.phone, purpose="register", code="000000")
        with self.assertRaises(ValidationError):
            verify_sms_code(phone=self.phone, purpose="register", code="009876")

    def test_concurrent_send_during_network_call_is_rejected_before_dispatch(self):
        entered, release = Event(), Event()

        def slow_send(request):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("unit test barrier")
            return accepted_response()

        self.client.SendSms.side_effect = slow_send
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.send)
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(cache.get(_code_key(self.phone, "register")))
                with self.assertRaises(ValidationError):
                    self.send("login")
            finally:
                release.set()
            self.assertIsNone(first.result(timeout=5).debug_code)
        self.client.SendSms.assert_called_once()

    def test_late_sender_cannot_overwrite_newer_challenge(self):
        def newer_send(request):
            cache.set(_cooldown_key(self.phone), "new-reservation", 60)
            cache.set(_code_key(self.phone, "register"), "654321", 300)
            return accepted_response()

        self.client.SendSms.side_effect = newer_send
        with self.assertRaises(SmsDeliveryUnavailable):
            self.send()
        self.assertEqual(cache.get(_code_key(self.phone, "register")), "654321")

    def test_invalid_phone_or_purpose_never_dispatches(self):
        for phone, purpose in (("invalid", "login"), ("13800000801", "arbitrary")):
            with self.subTest(phone=phone, purpose=purpose), self.assertRaises(ValidationError):
                send_sms_code(phone=phone, purpose=purpose)
        self.client_class.assert_not_called()

    def test_readonly_configuration_command_never_sends_or_prints_credentials(self):
        output = StringIO()
        call_command("check_tencent_sms", stdout=output)
        self.assertIn("本地短信配置完整", output.getvalue())
        self.assertNotIn("unit-secret", output.getvalue())
        self.client_class.assert_not_called()
        with override_settings(TENCENT_SMS_TEMPLATE_ID=""):
            with self.assertRaises(CommandError):
                call_command("check_tencent_sms", stdout=output)
        self.client_class.assert_not_called()

    def test_readonly_check_covers_scene_overrides_and_default_fallback(self):
        with override_settings(TENCENT_SMS_TEMPLATE_ID_LOGIN="invalid"):
            with self.assertRaisesMessage(CommandError, "用途：login"):
                call_command("check_tencent_sms", stdout=StringIO())
        # A deployment can also supply all six explicit templates, without a default.
        scene_settings = {setting: "9001" for setting in TEMPLATE_SETTINGS.values()}
        with override_settings(TENCENT_SMS_TEMPLATE_ID="", **scene_settings):
            output = StringIO()
            call_command("check_tencent_sms", stdout=output)
            self.assertIn("全部 7 个用途", output.getvalue())
        self.client_class.assert_not_called()


@override_settings(**SMS_SETTINGS)
class TencentSmsApiTests(APITestCase):
    phone = "13800000801"

    def setUp(self):
        cache.clear()
        client_patch = patch("accounts.tencent_sms.SmsClient")
        self.gateway = client_patch.start().return_value.SendSms
        self.addCleanup(client_patch.stop)
        self.gateway.side_effect = lambda request: accepted_response(
            phone=request.PhoneNumberSet[0][3:],
        )
        random_patch = patch("accounts.services.secrets.randbelow", return_value=9876)
        random_patch.start()
        self.addCleanup(random_patch.stop)

    def test_production_registration_and_login_without_debug_response(self):
        response = self.client.post("/api/v1/auth/sms-codes/", {
            "phone": self.phone, "purpose": "register", "code": "111111",
            "template_id": "client-must-not-control-template",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], {"expires_in": 300, "retry_after": 60})
        self.assertEqual(self.gateway.call_args.args[0].TemplateId, "1234")
        registered = self.client.post("/api/v1/auth/register/", {
            "phone": self.phone, "code": "009876", "password": "test-pass-2026",
        }, format="json")
        self.assertEqual(registered.status_code, 201, registered.data)
        cache.delete(_cooldown_key(self.phone))
        sent = self.client.post("/api/v1/auth/sms-codes/", {
            "phone": self.phone, "purpose": "login",
        }, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        logged_in = self.client.post("/api/v1/auth/login/sms/", {
            "phone": self.phone, "code": "009876",
        }, format="json")
        self.assertEqual(logged_in.status_code, 200, logged_in.data)
        replay = self.client.post("/api/v1/auth/login/sms/", {
            "phone": self.phone, "code": "009876",
        }, format="json")
        self.assertEqual(replay.status_code, 400)

    @override_settings(TENCENT_SMS_TEMPLATE_ID_LOGIN="9002")
    def test_api_selects_scene_server_side_and_ignores_client_template(self):
        User.objects.create_user(phone=self.phone, password=None)
        response = self.client.post("/api/v1/auth/sms-codes/", {
            "phone": self.phone, "purpose": "login", "template_id": "7777",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.gateway.call_args.args[0].TemplateId, "9002")
        cache.delete(_cooldown_key(self.phone))
        response = self.client.post("/api/v1/auth/sms-codes/", {
            "phone": self.phone, "purpose": "reset_password", "template_id": "7777",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.gateway.call_args.args[0].TemplateId, "1234")

    def test_provider_failure_returns_safe_503_and_no_usable_code(self):
        self.gateway.side_effect = TencentCloudSDKException("AuthFailure", "sensitive body")
        response = self.client.post("/api/v1/auth/sms-codes/", {
            "phone": self.phone, "purpose": "register",
        }, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sensitive", str(response.data))
        self.assertNotIn("debug_code", str(response.data))
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))

    def test_reset_initial_password_and_phone_change_reuse_gateway(self):
        user = User.objects.create_user(phone=self.phone, password=None)
        cases = [
            ("/api/v1/auth/sms-codes/", {"phone": self.phone, "purpose": "reset_password"},
             "reset_password", self.phone, False),
            ("/api/v1/auth/password/initial/code/", {"phone": "13800000899"},
             "initial_password", self.phone, True),
            ("/api/v1/auth/phone/change/code/", {"target": "current", "phone": "13800000899"},
             "change_phone_current", self.phone, True),
            ("/api/v1/auth/phone/change/code/", {"target": "new", "new_phone": "13800000802"},
             "change_phone_new", "13800000802", True),
        ]
        for path, data, purpose, phone, authenticated in cases:
            with self.subTest(path=path, purpose=purpose):
                cache.clear()
                self.client.force_authenticate(user=user if authenticated else None)
                response = self.client.post(path, data, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertNotIn("debug_code", response.data["data"])
                self.assertEqual(self.gateway.call_args.args[0].PhoneNumberSet, [f"+86{phone}"])
                verify_sms_code(phone=phone, purpose=purpose, code="009876")

    def test_wechat_binding_requires_ticket_then_uses_same_sms_gateway(self):
        path = "/api/v1/auth/login/wechat/bind/sms/"
        invalid = self.client.post(path, {"ticket": "invalid", "phone": self.phone}, format="json")
        self.assertEqual(invalid.status_code, 400)
        self.gateway.assert_not_called()
        ticket = _issue_ticket(WechatIdentity("official_account", "wx-unit", "unit-openid", ""))
        response = self.client.post(path, {"ticket": ticket, "phone": self.phone}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("debug_code", response.data["data"])
        verify_sms_code(phone=self.phone, purpose="wechat_bind", code="009876")
