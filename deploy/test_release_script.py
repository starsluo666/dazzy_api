"""Exercise release failure paths with a fake Docker CLI, without touching a daemon."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else None)
FAKE_DOCKER = r"""#!/usr/bin/env bash
set -eu
printf '%s|%s\n' "${DAZZY_IMAGE:-none}" "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  'compose version --short') echo 2.24.0 ;;
  'info --format '{{.OSType}}) echo linux ;;
  *' build api') [[ "${FAIL_BUILD:-0}" != 1 ]] ;;
  *' migrate --check') [[ "${PENDING_MIGRATIONS:-0}" != 1 ]] ;;
  *' up -d '*) [[ "${FAIL_START:-0}" != 1 ]] ;;
esac
"""


@unittest.skipUnless(BASH and Path(BASH).is_file(), "Bash is required")
class ReleaseScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dazzy-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        shutil.copyfile(ROOT / "deploy-docker.sh", self.project / "deploy-docker.sh")
        (self.project / ".env.docker").touch()
        bindir = self.project / "bin"
        bindir.mkdir()
        for name, contents in (("docker", FAKE_DOCKER), ("flock", "#!/usr/bin/env bash\nexit 0\n")):
            path = bindir / name
            path.write_text(contents, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.state = self.project / ".deploy"
        self.state.mkdir()
        (self.state / "current-image").write_text("dazzy-api:old\n")
        self.log = self.project / "docker.log"
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": str(bindir) + os.pathsep + self.env["PATH"],
                "FAKE_DOCKER_LOG": str(self.log).replace("\\", "/"),
                "MSYS_NO_PATHCONV": "1",
            }
        )
        for key in ("DAZZY_COMPOSE_ENV", "FAIL_BUILD", "FAIL_START", "PENDING_MIGRATIONS"):
            self.env.pop(key, None)

    def run_script(self, *args, **env):
        return subprocess.run(
            [BASH, "./deploy-docker.sh", *args],
            cwd=self.project,
            env={**self.env, **env},
            capture_output=True,
            timeout=40,
        )

    def calls(self):
        return self.log.read_text(encoding="utf-8")

    def assert_old_release(self):
        self.assertEqual((self.state / "current-image").read_text().strip(), "dazzy-api:old")

    def test_build_failure_never_stops_live_services(self):
        result = self.run_script("deploy", FAIL_BUILD="1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(" stop ", self.calls())
        self.assertNotIn(" up -d ", self.calls())
        self.assert_old_release()

    def test_pending_migrations_require_explicit_option_before_stop(self):
        result = self.run_script("deploy", PENDING_MIGRATIONS="1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(" stop ", self.calls())
        self.assertNotIn("migrate --noinput", self.calls())
        self.assert_old_release()

    def test_migration_runs_once_after_services_stop(self):
        result = self.run_script("deploy", "--migrate", PENDING_MIGRATIONS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls.count("migrate --noinput"), 1)
        self.assertLess(calls.index(" stop beat"), calls.index(" stop worker"))
        self.assertLess(calls.index(" stop api"), calls.index("migrate --noinput"))
        self.assertLess(calls.index("migrate --noinput"), calls.index(" up -d "))
        self.assertEqual((self.state / "previous-image").read_text().strip(), "dazzy-api:old")
        self.assertNotEqual((self.state / "current-image").read_text().strip(), "dazzy-api:old")

    def test_health_failure_does_not_record_success_or_auto_rollback(self):
        result = self.run_script("deploy", FAIL_START="1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assert_old_release()
        self.assertFalse((self.state / "releases.log").exists())
        self.assertEqual(self.calls().count(" up -d "), 1)

    def test_rollback_reuses_image_without_building_or_migrating(self):
        result = self.run_script("rollback", "dazzy-api:previous")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(" build ", self.calls())
        self.assertNotIn("migrate --noinput", self.calls())
        self.assertIn("dazzy-api:previous|", self.calls())
        self.assertEqual((self.state / "current-image").read_text().strip(), "dazzy-api:previous")


if __name__ == "__main__":
    unittest.main()
