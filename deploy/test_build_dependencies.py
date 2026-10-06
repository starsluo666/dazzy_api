"""Offline build configuration/timeout checks: no Docker, APT, or network writes."""

import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy.build_dependencies import SYSTEM_PACKAGES, configure_apt, install, mirror_url


ROOT = Path(__file__).resolve().parents[1]
SOURCES = """Types: deb
URIs: http://deb.debian.org/debian
Suites: bookworm bookworm-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://deb.debian.org/debian-security
Suites: bookworm-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""


class BuildDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dazzy-build-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "sources.list.d").mkdir()
        (self.root / "apt.conf.d").mkdir()
        self.sources = self.root / "sources.list.d/debian.sources"
        self.sources.write_text(SOURCES, encoding="utf-8")
        self.cleaner = self.root / "apt.conf.d/docker-clean"
        self.cleaner.write_text("build image cache cleaner", encoding="utf-8")

    def test_default_https_sources_and_cached_packages_keep_signature_checks(self):
        configure_apt({}, self.root)
        actual = self.sources.read_text(encoding="utf-8")
        self.assertEqual(actual, SOURCES.replace("http://", "https://"))
        self.assertFalse(self.cleaner.exists())
        config = (self.root / "apt.conf.d/99dazzy-build").read_text(encoding="utf-8")
        for setting in ('Keep-Downloaded-Packages "true"', 'Retries "3"', 'Timeout "30"'):
            self.assertIn(setting, config)
        self.assertNotIn("Verify-Peer", config)
        self.assertNotIn("AllowUnauthenticated", config)

    def test_mirrors_are_independent_and_do_not_change_distribution_or_keys(self):
        configure_apt({
            "DEBIAN_MIRROR": "https://mirror.example.test/debian/",
            "DEBIAN_SECURITY_MIRROR": "https://security.example.test/debian-security",
        }, self.root)
        expected = SOURCES.replace(
            "http://deb.debian.org/debian-security", "https://security.example.test/debian-security",
        ).replace("http://deb.debian.org/debian", "https://mirror.example.test/debian")
        self.assertEqual(self.sources.read_text(encoding="utf-8"), expected)

    def test_invalid_mirrors_fail_before_writing_configuration(self):
        for mirror in (
            "", "file:///etc/passwd", "https://", "https://user:secret@example.test/debian",
            "https://example.test/debian\nTrusted: yes", "https://example.test/a?token=secret",
            "https://example.test/a#fragment", "https://example.test:bad/debian",
            "https://example.test/a?", "https://example.test/a#", "https://[invalid/debian",
        ):
            with self.subTest(mirror=mirror), self.assertRaises(ValueError):
                configure_apt({"DEBIAN_MIRROR": mirror}, self.root)
        self.assertEqual(self.sources.read_text(encoding="utf-8"), SOURCES)
        self.assertTrue(self.cleaner.exists())
        self.assertEqual(mirror_url("https://example.test:8443/debian/"),
                         "https://example.test:8443/debian")

    def test_changed_base_sources_fail_closed(self):
        for sources in (
            SOURCES.replace("deb.debian.org", "unexpected.example.test"),
            SOURCES.split("\n\n")[0],
        ):
            self.sources.write_text(sources, encoding="utf-8")
            with self.assertRaises(ValueError):
                configure_apt({}, self.root)
            self.assertEqual(self.sources.read_text(encoding="utf-8"), sources)
            self.assertTrue(self.cleaner.exists())

    @patch("deploy.build_dependencies.subprocess.run")
    def test_python_keeps_locked_versions_and_has_total_and_http_timeouts(self, run):
        run.return_value = subprocess.CompletedProcess([], 0)
        self.assertEqual(install("python", env={}), 0)
        self.assertEqual(run.call_args.args[0], [
            "timeout", "--kill-after=30s", "1200s", "uv", "sync",
            "--locked", "--no-dev", "--no-install-project",
        ])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["UV_HTTP_TIMEOUT"], "30")
        self.assertEqual(env["UV_HTTP_RETRIES"], "3")
        self.assertEqual(env["UV_HTTP_CONNECT_TIMEOUT"], "10")
        self.assertNotIn("UV_DEFAULT_INDEX", env)

    @patch("deploy.build_dependencies.subprocess.run")
    def test_apt_keeps_all_runtime_dependencies_and_fails_on_partial_update(self, run):
        run.return_value = subprocess.CompletedProcess([], 0)
        self.assertEqual(install("apt", env={"BUILD_DEPENDENCY_TIMEOUT": "1800"},
                                 apt_root=self.root), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["timeout", "--kill-after=30s", "1800s", "sh", "-ec"])
        self.assertIn("apt-get update --error-on=any && ", command[-1])
        self.assertIn("--no-install-recommends", command[-1])
        for package in SYSTEM_PACKAGES:
            self.assertIn(package, command[-1])
        self.assertEqual(run.call_args.kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")

    @patch("deploy.build_dependencies.subprocess.run")
    def test_invalid_timeouts_never_start_install_or_disable_the_limit(self, run):
        for value in ("0", "0000", "-1", "59", "7201", "1h", "1.5", "", "1200; false"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                install("apt", env={"BUILD_DEPENDENCY_TIMEOUT": value}, apt_root=self.root)
        run.assert_not_called()
        self.assertEqual(self.sources.read_text(encoding="utf-8"), SOURCES)

    @patch("deploy.build_dependencies.subprocess.run")
    def test_failure_and_timeout_codes_are_not_swallowed(self, run):
        for code in (1, 100, 124, 137):
            run.return_value = subprocess.CompletedProcess([], code)
            output = io.StringIO()
            with self.subTest(code=code), contextlib.redirect_stderr(output):
                self.assertEqual(install("python", env={}), code)
            self.assertIn("timed out" if code in (124, 137) else "failed", output.getvalue())

    def test_docker_layers_reuse_caches_and_keep_source_code_after_dependencies(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(dockerfile.count("sharing=locked"), 3)
        for target in ("/root/.cache/uv", "/var/cache/apt", "/var/lib/apt/lists"):
            self.assertIn(f"target={target},sharing=locked", dockerfile)
        self.assertNotIn("apt-get clean", dockerfile)
        self.assertNotIn("rm -rf /var/lib/apt/lists", dockerfile)
        self.assertLess(dockerfile.index("build_dependencies.py apt"), dockerfile.index("COPY . ."))
        self.assertLess(dockerfile.index("build_dependencies.py python"), dockerfile.index("COPY . ."))
        self.assertIn("USER dazzy", dockerfile)
        compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
        for name in ("DEBIAN_MIRROR", "DEBIAN_SECURITY_MIRROR", "BUILD_DEPENDENCY_TIMEOUT"):
            self.assertIn(f"{name}: ${{DAZZY_{name}:-", compose)
            self.assertIn(f"ARG {name}=", dockerfile)


if __name__ == "__main__":
    unittest.main()
