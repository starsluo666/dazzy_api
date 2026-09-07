from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from accounts.models import User


class AdminCookieAuthenticationTests(APITestCase):
    phone = "19900009991"
    password = "admin-test-password"
    cookie_name = "dazzy_admin_refresh"

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            phone=cls.phone,
            password=cls.password,
            nickname="Cookie 管理员",
        )

    def login(self):
        return self.client.post(
            "/api/v1/admin/auth/login/",
            {"phone": self.phone, "password": self.password},
            format="json",
        )

    def assert_cookie_cleared(self, response):
        self.assertIn(self.cookie_name, response.cookies)
        self.assertEqual(response.cookies[self.cookie_name]["max-age"], 0)

    def test_login_returns_only_access_and_sets_scoped_http_only_cookie(self):
        response = self.login()

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data["data"])
        self.assertNotIn("refresh", response.data["data"])
        cookie = response.cookies[self.cookie_name]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/api/v1/admin/auth/")
        self.assertGreater(int(cookie["max-age"]), 0)
        self.assertEqual(response["Cache-Control"], "no-store")

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['data']['access']}"
        )
        self.assertEqual(self.client.get("/api/v1/admin/me/").status_code, 200)

    @override_settings(ADMIN_REFRESH_COOKIE_SECURE=True)
    def test_production_cookie_is_secure(self):
        response = self.login()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies[self.cookie_name]["secure"])

    def test_refresh_rotates_cookie_and_old_refresh_cannot_be_replayed(self):
        login = self.login()
        old_refresh = login.cookies[self.cookie_name].value

        refreshed = self.client.post("/api/v1/admin/auth/refresh/", {}, format="json")

        self.assertEqual(refreshed.status_code, 200)
        self.assertIn("access", refreshed.data["data"])
        self.assertNotIn("refresh", refreshed.data["data"])
        self.assertNotEqual(refreshed.cookies[self.cookie_name].value, old_refresh)

        replay_client = APIClient()
        replay_client.cookies[self.cookie_name] = old_refresh
        replay = replay_client.post("/api/v1/admin/auth/refresh/", {}, format="json")
        self.assertEqual(replay.status_code, 401)
        self.assert_cookie_cleared(replay)

    def test_refresh_rejects_changed_auth_version_and_clears_cookie(self):
        self.login()
        self.admin.auth_version += 1
        self.admin.save(update_fields=("auth_version",))

        response = self.client.post("/api/v1/admin/auth/refresh/", {}, format="json")

        self.assertEqual(response.status_code, 401)
        self.assert_cookie_cleared(response)

    def test_refresh_rechecks_backoffice_access(self):
        self.login()
        self.admin.is_superuser = False
        self.admin.is_staff = False
        self.admin.save(update_fields=("is_superuser", "is_staff"))

        response = self.client.post("/api/v1/admin/auth/refresh/", {}, format="json")

        self.assertEqual(response.status_code, 403)
        self.assert_cookie_cleared(response)

    def test_logout_blacklists_refresh_and_clears_cookie(self):
        login = self.login()
        refresh_value = login.cookies[self.cookie_name].value

        response = self.client.post("/api/v1/admin/auth/logout/", {}, format="json")

        self.assertEqual(response.status_code, 204)
        self.assert_cookie_cleared(response)
        replay_client = APIClient()
        replay_client.cookies[self.cookie_name] = refresh_value
        replay = replay_client.post("/api/v1/admin/auth/refresh/", {}, format="json")
        self.assertEqual(replay.status_code, 401)

    def test_user_without_backoffice_access_cannot_create_admin_session(self):
        user = User.objects.create_user(
            phone="19900009992", password=self.password, nickname="普通用户"
        )

        response = self.client.post(
            "/api/v1/admin/auth/login/",
            {"phone": user.phone, "password": self.password},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn(self.cookie_name, response.cookies)

    def test_existing_app_login_contract_is_unchanged(self):
        response = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": self.password},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data["data"])
        self.assertIn("refresh", response.data["data"])
        self.assertNotIn(self.cookie_name, response.cookies)
