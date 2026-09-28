import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from deploy import healthcheck


def isolated_environment():
    """Never point a production-settings test at the user's business database."""
    env = os.environ.copy()
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "DJANGO_SECRET_KEY": "deployment-test-only-" + "x" * 60,
            "DJANGO_ALLOWED_HOSTS": " api.example.test , 127.0.0.1 ",
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": "1",
            "POSTGRES_DATABASE": "unused",
            "POSTGRES_USER": "unused",
            "POSTGRES_PASSWORD": "unused",
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": "1",
            "COS_REGION": "unused",
            "COS_BUCKET": "unused",
            "COS_BASE_URL": "https://example.invalid",
            "TENCENT_CLOUD_SECRET_ID": "unused",
            "TENCENT_CLOUD_SECRET_KEY": "unused",
            "TENCENT_MAP_WEB_SERVICE_KEY": "unused",
            "TENCENT_MAP_WEB_SERVICE_SK": "unused",
        }
    )
    return env


class ProductionSettingsTests(unittest.TestCase):
    def run_python(self, code, **environment):
        env = isolated_environment()
        env.update(environment)
        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_https_proxy_and_real_health_endpoint(self):
        result = self.run_python("""
import django
django.setup()
from django.conf import settings
from django.test import Client
c = Client(HTTP_HOST='api.example.test')
assert c.get('/api/v1/health/').status_code == 301
r = c.get('/api/v1/health/', HTTP_X_FORWARDED_PROTO='https')
assert r.status_code == 200, r.status_code
assert r.json()['data']['status'] == 'ok'
assert not settings.DEBUG
assert settings.ADMIN_REFRESH_COOKIE_SECURE
assert 'config.authentication.DevelopmentUserAuthentication' not in settings.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']
assert settings.MIDDLEWARE[1] == 'whitenoise.middleware.WhiteNoiseMiddleware'
assert settings.DATABASES['default']['OPTIONS']['connect_timeout'] == 5
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_collected_admin_static_assets_are_served_in_production(self):
        result = self.run_python("""
import tempfile
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.test import Client
with tempfile.TemporaryDirectory(prefix='dazzy-static-test-') as directory:
    settings.STATIC_ROOT = directory
    call_command('collectstatic', interactive=False, verbosity=0)
    from django.contrib.staticfiles.storage import staticfiles_storage
    url = staticfiles_storage.url('admin/css/base.css')
    assert url != '/static/admin/css/base.css', url
    response = Client(HTTP_HOST='api.example.test').get(url, HTTP_X_FORWARDED_PROTO='https')
    assert response.status_code == 200, response.status_code
    assert 'text/css' in response.headers['Content-Type']
    assert 'immutable' in response.headers['Cache-Control']
    response.close()
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_production_configuration_fails_before_serving(self):
        for invalid in (
            {"DJANGO_SECRET_KEY": ""},
            {"DJANGO_ALLOWED_HOSTS": "*"},
            {"DJANGO_ALLOWED_HOSTS": " "},
        ):
            with self.subTest(invalid=invalid):
                result = self.run_python("import config.settings.production", **invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ImproperlyConfigured", result.stderr)


class ProbeTests(unittest.TestCase):
    def probe_response(self, body, status=200):
        response = io.StringIO(json.dumps(body))
        response.status = status
        return response

    @patch.dict(os.environ, {"DJANGO_ALLOWED_HOSTS": "api.example.test,localhost"})
    @patch("deploy.healthcheck.build_opener")
    def test_probe_uses_proxy_headers_and_checks_both_dependencies(self, factory):
        factory.return_value.open.return_value = self.probe_response(
            {
                "data": {"status": "ready", "checks": {"database": True, "cache": True}},
            }
        )
        healthcheck.check_api()
        request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Host"), "api.example.test")
        self.assertEqual(request.get_header("X-forwarded-proto"), "https")
        self.assertIsNone(
            healthcheck.NoRedirect().redirect_request(
                request,
                None,
                301,
                "",
                {},
                "https://example.invalid",
            )
        )

    @patch.dict(os.environ, {"DJANGO_ALLOWED_HOSTS": "api.example.test"})
    @patch("deploy.healthcheck.build_opener")
    def test_redirect_or_partial_readiness_cannot_pass(self, factory):
        for status, checks in (
            (301, {"database": True, "cache": True}),
            (200, {"database": True, "cache": False}),
            (200, {}),
        ):
            with self.subTest(status=status, checks=checks):
                factory.return_value.open.return_value = self.probe_response(
                    {
                        "data": {"status": "ready", "checks": checks},
                    },
                    status,
                )
                with self.assertRaises(RuntimeError):
                    healthcheck.check_api()

    @patch("deploy.healthcheck.socket.gethostname", return_value="worker-1")
    def test_worker_probe_does_not_accept_another_workers_reply(self, _hostname):
        module = MagicMock()
        inspector = module.app.control.inspect.return_value
        with patch.dict(sys.modules, {"config.celery": module}):
            inspector.ping.return_value = {"celery@worker-2": {"ok": "pong"}}
            with self.assertRaises(RuntimeError):
                healthcheck.check_worker()
            inspector.ping.return_value = {"celery@worker-1": {"ok": "pong"}}
            healthcheck.check_worker()
        module.app.control.inspect.assert_called_with(destination=["celery@worker-1"], timeout=5)


if __name__ == "__main__":
    unittest.main()
