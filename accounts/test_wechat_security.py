from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.query import QuerySet
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from .models import User, WechatLoginIdentity, WechatUnionIdentity
from .services import send_sms_code
from .views import auth_payload
from .wechat_login import WechatIdentity, _attach_identity, _issue_ticket, resolve_login


@override_settings(DEBUG=True, SMS_DEVELOPMENT_CODE="123456", SMS_CODE_RESEND_SECONDS=0)
class InitialPasswordTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(phone="13800000701", password=None)
        self.old_session = auth_payload(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.old_session['access']}")

    def test_verified_initial_password_unlocks_security_actions_and_revokes_old_session(self):
        sent = self.client.post("/api/v1/auth/password/initial/code/", {}, format="json")
        self.assertEqual(sent.status_code, 200)
        configured = self.client.post(
            "/api/v1/auth/password/initial/",
            {
                "code": "123456",
                "new_password": "initial-pass-2026",
            },
            format="json",
        )
        self.assertEqual(configured.status_code, 200, configured.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("initial-pass-2026"))
        self.assertEqual(self.user.auth_version, 2)
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {configured.data['data']['access']}")
        changed = self.client.post(
            "/api/v1/auth/password/change/",
            {
                "current_password": "initial-pass-2026",
                "new_password": "changed-pass-2026",
            },
            format="json",
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {changed.data['data']['access']}")
        logged_out = self.client.post(
            "/api/v1/auth/sessions/logout-others/",
            {
                "current_password": "changed-pass-2026",
            },
            format="json",
        )
        self.assertEqual(logged_out.status_code, 200, logged_out.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {logged_out.data['data']['access']}")
        with patch("accounts.account_closure.account_closure_blockers", return_value=[]):
            closed = self.client.post(
                "/api/v1/auth/account/close/",
                {
                    "current_password": "changed-pass-2026",
                },
                format="json",
            )
        self.assertEqual(closed.status_code, 200, closed.data)

    def test_code_for_other_phone_or_purpose_cannot_set_initial_password(self):
        send_sms_code(phone="13800000702", purpose="initial_password")
        send_sms_code(phone=self.user.phone, purpose="reset_password")
        response = self.client.post(
            "/api/v1/auth/password/initial/",
            {
                "code": "123456",
                "new_password": "initial-pass-2026",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_usable_password())

    def test_code_destination_is_current_account_and_existing_password_cannot_be_overwritten(self):
        response = self.client.post(
            "/api/v1/auth/password/initial/code/",
            {
                "phone": "13800000702",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(cache.get("auth:sms-code:initial_password:13800000702"))
        self.assertIsNotNone(cache.get(f"auth:sms-code:initial_password:{self.user.phone}"))
        self.user.set_password("existing-pass-2026")
        self.user.save(update_fields=("password",))
        response = self.client.post(
            "/api/v1/auth/password/initial/",
            {
                "code": "123456",
                "new_password": "overwrite-pass-2026",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post("/api/v1/auth/password/initial/code/").status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("existing-pass-2026"))

    @override_settings(DEBUG=False)
    def test_initial_password_sms_keeps_production_gateway_safety_gate(self):
        self.assertEqual(self.client.post("/api/v1/auth/password/initial/code/").status_code, 503)


@override_settings(WECHAT_CROSS_CHANNEL_UNIONID_ENABLED=True)
class WechatUnionOwnershipTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(phone="13800000711", password=None)
        self.other = User.objects.create_user(phone="13800000712", password=None)
        self.h5 = WechatIdentity("official_account", "wx-official", "h5-openid", "same-union")
        self.app = WechatIdentity("mobile_app", "wx-mobile", "app-openid", "same-union")

    def test_both_channels_resolve_to_one_owner(self):
        _attach_identity(identity=self.h5, user=self.owner)
        self.assertEqual(resolve_login(_issue_ticket(self.app)).pk, self.owner.pk)
        self.assertEqual(WechatUnionIdentity.objects.count(), 1)
        self.assertEqual(WechatLoginIdentity.objects.filter(user=self.owner).count(), 2)

    def test_unique_owner_rejects_second_writer_even_when_precheck_is_stale(self):
        _attach_identity(identity=self.h5, user=self.owner)
        # Model the losing writer's earlier snapshot: its advisory existence
        # check did not see the winner. The unique ownership row still rejects it.
        with patch("django.db.models.query.QuerySet.exists", return_value=False):
            with self.assertRaises(ValidationError):
                _attach_identity(identity=self.app, user=self.other)
        self.assertFalse(WechatLoginIdentity.objects.filter(user=self.other).exists())
        self.assertEqual(
            WechatUnionIdentity.objects.get(unionid="same-union").user_id, self.owner.pk
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            WechatUnionIdentity.objects.create(unionid="same-union", user=self.other)

    def test_preexisting_conflicting_identities_fail_closed_without_claiming(self):
        for user, identity in ((self.owner, self.h5), (self.other, self.app)):
            WechatLoginIdentity.objects.create(
                user=user,
                channel=identity.channel,
                app_id=identity.app_id,
                openid=identity.openid,
                unionid=identity.unionid,
                authorized_at=timezone.now(),
            )
        with self.assertRaises(ValidationError):
            resolve_login(_issue_ticket(self.h5))
        self.assertFalse(WechatUnionIdentity.objects.exists())


@override_settings(WECHAT_CROSS_CHANNEL_UNIONID_ENABLED=True)
class WechatUnionConcurrencyTests(TransactionTestCase):
    def test_parallel_cross_channel_claims_have_exactly_one_owner(self):
        if connection.vendor != "postgresql":
            self.skipTest(
                "Requires an isolated PostgreSQL test database for real unique-key contention"
            )
        users = [
            User.objects.create_user(phone=phone, password=None)
            for phone in ("13800000721", "13800000722")
        ]
        identities = [
            WechatIdentity("official_account", "wx-official", "h5-openid", "same-union"),
            WechatIdentity("mobile_app", "wx-mobile", "app-openid", "same-union"),
        ]
        barrier = Barrier(2)
        original_exists = QuerySet.exists

        def stale_precheck(queryset):
            result = original_exists(queryset)
            if queryset.model is WechatLoginIdentity:
                barrier.wait(timeout=10)
            return result

        def claim(index):
            close_old_connections()
            try:
                user = User.objects.get(pk=users[index].pk)
                try:
                    _attach_identity(identity=identities[index], user=user)
                    return "accepted"
                except ValidationError:
                    return "rejected"
            finally:
                close_old_connections()

        with (
            patch.object(QuerySet, "exists", stale_precheck),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(claim, (0, 1)))
        self.assertEqual(sorted(results), ["accepted", "rejected"])
        self.assertEqual(WechatLoginIdentity.objects.count(), 1)
        self.assertEqual(WechatUnionIdentity.objects.count(), 1)
