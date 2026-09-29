from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APITestCase

from .account_closure import lock_active_user_for_business, process_account_closure, request_account_closure
from .business_days import closure_deadline
from .models import AccountClosureRequest, User, WechatMiniProgramIdentity
from .serializers import CloseAccountSerializer
from .services import send_sms_code
from .tasks import process_due_account_closures
from .views import auth_payload
from .wechat_login import WechatIdentity, _attach_identity, _issue_ticket

SHANGHAI = ZoneInfo("Asia/Shanghai")


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


class ClosureCalendarTests(SimpleTestCase):
    def test_normal_week_excludes_request_day_and_weekend(self):
        self.assertEqual(
            closure_deadline(instant("2026-07-06T14:20:00")),
            instant("2026-07-13T14:20:00"),
        )

    def test_national_day_and_makeup_saturday_are_counted_correctly(self):
        self.assertEqual(
            closure_deadline(instant("2026-09-29T14:20:00")),
            instant("2026-10-12T14:20:00"),
        )

    def test_mid_autumn_and_makeup_sunday(self):
        self.assertEqual(
            closure_deadline(instant("2026-09-18T14:20:00")),
            instant("2026-09-24T14:20:00"),
        )
        self.assertEqual(
            closure_deadline(instant("2026-09-24T14:20:00")),
            instant("2026-10-09T14:20:00"),
        )

    def test_shanghai_date_used_instead_of_utc_date(self):
        self.assertEqual(
            closure_deadline(datetime.fromisoformat("2026-09-28T17:20:00+00:00")),
            instant("2026-10-12T01:20:00"),
        )

    @override_settings(ACCOUNT_CLOSURE_CALENDARS={})
    def test_unreviewed_year_is_not_guessed(self):
        with self.assertRaisesMessage(ValidationError, "工作日历尚未配置"):
            closure_deadline(instant("2026-12-30T12:00:00"))

    @override_settings(ACCOUNT_CLOSURE_CALENDARS={
        "2027": {"holidays": ["2027-01-01"], "working_weekends": []},
    })
    def test_reviewed_calendar_override_supports_cross_year_deadline(self):
        self.assertEqual(
            closure_deadline(instant("2026-12-30T12:00:00")),
            instant("2027-01-07T12:00:00"),
        )

    @override_settings(ACCOUNT_CLOSURE_CALENDARS={
        "2026": {"holidays": ["2026-07-07"], "working_weekends": ["2026-07-07"]},
    })
    def test_conflicting_calendar_fails_safely(self):
        with self.assertRaises(ImproperlyConfigured):
            closure_deadline(instant("2026-07-06T12:00:00"))


@override_settings(DEBUG=True, SMS_DEVELOPMENT_CODE="123456", SMS_CODE_RESEND_SECONDS=0)
class DeferredClosureTests(APITestCase):
    requested_at = instant("2026-09-29T14:20:00")
    password = "closure-pass-2026"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(phone="13800000401", password=self.password)
        self.session = auth_payload(self.user)
        self.clock = patch("accounts.account_closure.timezone", SimpleNamespace(now=lambda: self.requested_at))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def submit(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.session['access']}")
        result = self.client.post(
            "/api/v1/auth/account/close/", {"current_password": self.password}, format="json",
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.closure = AccountClosureRequest.objects.get(user=self.user, status="pending")
        return result

    def login(self, path="password", **data):
        self.client.credentials()
        return self.client.post(f"/api/v1/auth/login/{path}/", data, format="json")

    def test_request_returns_deadline_and_invalidates_access_refresh_and_business(self):
        result = self.submit()
        self.assertFalse(result.data["data"]["closed"])
        self.assertEqual(result.data["data"]["status"], "pending")
        self.assertEqual(result.data["data"]["execute_after"], "2026-10-12T14:20:00+08:00")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.account_status, User.AccountStatus.CLOSURE_PENDING)
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 401)
        self.client.credentials()
        refreshed = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": self.session["refresh"]}, format="json",
        )
        self.assertEqual(refreshed.status_code, 401)
        with self.assertRaisesMessage(PermissionDenied, "不可发起新的业务"):
            lock_active_user_for_business(self.user)
        self.closure.refresh_from_db()
        self.assertEqual(self.closure.status, AccountClosureRequest.Status.PENDING)

    def test_wrong_password_does_not_create_request(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.session['access']}")
        result = self.client.post(
            "/api/v1/auth/account/close/", {"current_password": "wrong-pass-2026"}, format="json",
        )
        self.assertEqual(result.status_code, 400)
        self.assertEqual(AccountClosureRequest.objects.count(), 0)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_missing_next_year_calendar_leaves_account_and_session_operable(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.session['access']}")
        with patch("accounts.account_closure.timezone", SimpleNamespace(now=lambda: instant("2026-12-30T12:00:00"))):
            result = self.client.post(
                "/api/v1/auth/account/close/", {"current_password": self.password}, format="json",
            )
        self.assertEqual(result.status_code, 400)
        self.assertEqual(AccountClosureRequest.objects.count(), 0)
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 200)

    def test_admin_password_login_does_not_bypass_waiting_period(self):
        self.user.is_superuser = True
        self.user.save(update_fields=("is_superuser",))
        self.submit()
        self.client.credentials()
        result = self.client.post("/api/v1/admin/auth/login/", {
            "phone": self.user.phone, "password": self.password,
        }, format="json")
        self.assertEqual(result.status_code, 400, result.data)
        self.closure.refresh_from_db()
        self.assertEqual(self.closure.status, AccountClosureRequest.Status.PENDING)

    def test_django_session_created_before_request_cannot_use_account(self):
        self.client.force_login(self.user)
        self.submit()
        self.client.credentials()
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 401)

    def test_duplicate_submission_cannot_extend_waiting_period(self):
        self.submit()
        serializer = CloseAccountSerializer(
            data={"current_password": self.password}, context={"request": SimpleNamespace(user=self.user)},
        )
        serializer.is_valid(raise_exception=True)
        with self.assertRaises(ValidationError):
            serializer.save()
        self.assertEqual(AccountClosureRequest.objects.count(), 1)
        self.closure.refresh_from_db()
        self.assertEqual(self.closure.requested_at, self.requested_at)

    def test_valid_password_login_withdraws_and_old_session_stays_invalid(self):
        self.submit()
        result = self.login(phone=self.user.phone, password=self.password)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data["data"]["closure_cancelled"])
        self.closure.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.closure.status, AccountClosureRequest.Status.CANCELLED)
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.account_status, User.AccountStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.session['access']}")
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {result.data['data']['access']}")
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 200)
        self.assertEqual(process_account_closure(self.user.pk, now=self.closure.execute_after), "skipped")

    def test_wrong_password_and_wrong_sms_never_withdraw(self):
        self.submit()
        self.assertEqual(self.login(phone=self.user.phone, password="wrong-pass-2026").status_code, 400)
        send_sms_code(phone=self.user.phone, purpose="login")
        self.assertEqual(self.login("sms", phone=self.user.phone, code="000000").status_code, 400)
        self.closure.refresh_from_db()
        self.assertEqual(self.closure.status, AccountClosureRequest.Status.PENDING)

    def test_sending_sms_and_resetting_password_do_not_withdraw_but_verified_sms_does(self):
        self.submit()
        self.client.credentials()
        send_sms_code(phone=self.user.phone, purpose="reset_password")
        result = self.client.post("/api/v1/auth/password/reset/", {
            "phone": self.user.phone, "code": "123456", "new_password": "reset-close-2026",
        }, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        send_sms_code(phone=self.user.phone, purpose="login")
        self.closure.refresh_from_db()
        self.assertEqual(self.closure.status, AccountClosureRequest.Status.PENDING)
        result = self.login("sms", phone=self.user.phone, code="123456")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data["data"]["closure_cancelled"])

    def test_waiting_period_prevents_phone_reregistration(self):
        self.submit()
        self.client.credentials()
        result = self.client.post("/api/v1/auth/sms-codes/", {
            "phone": self.user.phone, "purpose": "register",
        }, format="json")
        self.assertEqual(result.status_code, 400)
        self.assertEqual(User.objects.filter(phone=self.user.phone).count(), 1)

    def test_due_time_is_strict_even_if_worker_has_not_run(self):
        self.submit()
        with patch("accounts.account_closure.timezone", SimpleNamespace(now=lambda: self.closure.execute_after)):
            result = self.login(phone=self.user.phone, password=self.password)
        self.assertEqual(result.status_code, 400)
        self.assertIn("等待期已结束", str(result.data))
        self.closure.refresh_from_db()
        self.assertEqual(self.closure.status, AccountClosureRequest.Status.PENDING)

    def test_worker_never_closes_early_and_is_idempotent_after_deadline(self):
        self.submit()
        self.assertEqual(process_account_closure(
            self.user.pk, now=self.closure.execute_after - timedelta(microseconds=1),
        ), "skipped")
        self.assertEqual(process_account_closure(self.user.pk, now=self.closure.execute_after), "completed")
        self.assertEqual(process_account_closure(self.user.pk, now=self.closure.execute_after), "skipped")
        self.user.refresh_from_db()
        self.closure.refresh_from_db()
        self.assertEqual(self.user.account_status, User.AccountStatus.CLOSED)
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.closure.finished_at, self.closure.execute_after)
        self.assertEqual(self.login(phone=self.user.phone, password=self.password).status_code, 400)

    def test_new_unresolved_business_pauses_and_later_rechecks_before_closing(self):
        from wallets.models import UserWallet

        self.submit()
        wallet = UserWallet.objects.create(user=self.user, available_balance=100)
        self.assertEqual(process_account_closure(self.user.pk, now=self.closure.execute_after), "blocked")
        self.user.refresh_from_db()
        self.closure.refresh_from_db()
        self.assertEqual(self.user.account_status, User.AccountStatus.CLOSURE_PENDING)
        self.assertEqual(self.closure.blocking_items[0]["code"], "wallet_balance")
        # Simulate an already audited settlement by another workflow, not a waiver.
        wallet.available_balance = 0
        wallet.save(update_fields=("available_balance",))
        self.assertEqual(process_account_closure(self.user.pk, now=self.closure.execute_after), "completed")

    def test_task_catches_up_when_beat_restarts_after_deadline(self):
        self.submit()
        due_clock = SimpleNamespace(now=lambda: self.closure.execute_after + timedelta(days=1))
        with patch("accounts.account_closure.timezone", due_clock), patch("accounts.tasks.timezone", due_clock):
            self.assertEqual(process_due_account_closures(), {"completed": 1})
            self.assertEqual(process_due_account_closures(), {})

    def test_noninteractive_auth_payload_cannot_withdraw(self):
        self.submit()
        with self.assertRaises(ValidationError):
            auth_payload(self.user)
        self.closure.refresh_from_db()
        self.assertEqual(self.closure.status, AccountClosureRequest.Status.PENDING)

    def test_profile_request_authenticated_before_closure_cannot_overwrite_pending_state(self):
        self.submit()
        # Model an in-flight request whose authentication loaded an active User
        # just before the closure transaction committed.
        self.client.force_authenticate(self.user)
        result = self.client.patch("/api/v1/users/me/", {"nickname": "late-profile-change"}, format="json")
        self.assertEqual(result.status_code, 403, result.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.account_status, User.AccountStatus.CLOSURE_PENDING)
        self.assertFalse(self.user.is_active)
        self.assertNotEqual(self.user.nickname, "late-profile-change")

    def test_wechat_h5_and_app_verified_identities_withdraw_but_invalid_tickets_do_not(self):
        for channel in ("official_account", "mobile_app"):
            with self.subTest(channel=channel):
                identity = WechatIdentity(channel, f"wx-{channel}", f"openid-{channel}", "")
                _attach_identity(identity=identity, user=self.user)
                self.submit()
                self.client.credentials()
                invalid = self.client.post("/api/v1/auth/login/wechat/resolve/", {"ticket": "invalid"}, format="json")
                self.assertEqual(invalid.status_code, 400)
                result = self.client.post("/api/v1/auth/login/wechat/resolve/", {"ticket": _issue_ticket(identity)}, format="json")
                self.assertEqual(result.status_code, 200, result.data)
                self.assertTrue(result.data["data"]["session"]["closure_cancelled"])
                self.session = result.data["data"]["session"]
                self.user.refresh_from_db()

    @override_settings(WECHAT_CUSTOMER_MINI_PROGRAM_APP_ID="wx-mini-test", WECHAT_CUSTOMER_MINI_PROGRAM_APP_SECRET="test")
    @patch("accounts.serializers.exchange_login_code", return_value=("closure-openid", ""))
    def test_verified_miniprogram_login_withdraws(self, _exchange):
        WechatMiniProgramIdentity.objects.create(
            user=self.user, app_id="wx-mini-test", openid="closure-openid", authorized_at=self.requested_at,
        )
        self.submit()
        result = self.login("wechat-mini-program", client_type="customer", login_code="verified-code")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data["data"]["closure_cancelled"])


class ClosureConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency is tested with PostgreSQL.")
        self.user = User.objects.create_user(phone="13800000402", password="close-pass-2026")
        self.now = instant("2026-09-29T14:20:00")
        self.clock = patch("accounts.account_closure.timezone", SimpleNamespace(now=lambda: self.now))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def run_concurrently(self, functions):
        barrier = Barrier(len(functions))

        def run(function):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return function()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def request(self):
        try:
            request_account_closure(self.user, "close-pass-2026")
            return "accepted"
        except ValidationError:
            return "rejected"

    def test_concurrent_submissions_create_one_request(self):
        self.assertEqual(sorted(self.run_concurrently([self.request, self.request])), ["accepted", "rejected"])
        self.assertEqual(AccountClosureRequest.objects.count(), 1)

    def test_concurrent_workers_complete_only_once(self):
        closure = request_account_closure(self.user, "close-pass-2026")
        def process():
            return process_account_closure(self.user.pk, now=closure.execute_after)
        self.assertEqual(sorted(self.run_concurrently([process, process])), ["completed", "skipped"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.auth_version, 3)
        self.assertEqual(AccountClosureRequest.objects.get().status, AccountClosureRequest.Status.COMPLETED)

    def test_login_at_deadline_cannot_race_worker_and_reactivate_account(self):
        closure = request_account_closure(self.user, "close-pass-2026")
        self.now = closure.execute_after

        def login():
            try:
                auth_payload(self.user, interactive_login=True)
                return "accepted"
            except ValidationError:
                return "rejected"

        results = self.run_concurrently([
            login, lambda: process_account_closure(self.user.pk, now=self.now),
        ])
        self.assertEqual(results[0], "rejected")
        # A worker may skip a row temporarily locked by the denied login; retry.
        process_account_closure(self.user.pk, now=self.now)
        self.user.refresh_from_db()
        self.assertEqual(self.user.account_status, User.AccountStatus.CLOSED)
        self.assertFalse(self.user.is_active)
