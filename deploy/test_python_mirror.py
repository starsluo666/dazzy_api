"""Real uv smoke tests against a loopback index; never contact an external registry."""

import base64
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from zipfile import ZipFile

from deploy.build_dependencies import python_mirror_commands


ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")
PACKAGE = "dazzy_build_probe"
WHEEL = f"{PACKAGE}-1.0.0-py3-none-any.whl"


def make_test_wheel():
    """A tiny valid wheel, built entirely with the standard library for this test."""
    metadata = f"{PACKAGE}-1.0.0.dist-info"
    files = {
        f"{PACKAGE}/__init__.py": b'__version__ = "1.0.0"\n',
        f"{metadata}/METADATA": (
            "Metadata-Version: 2.1\nName: dazzy-build-probe\nVersion: 1.0.0\n"
        ).encode(),
        f"{metadata}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    records = []
    for name, contents in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).rstrip(b"=").decode()
        records.append(f"{name},sha256={digest},{len(contents)}\n")
    files[f"{metadata}/RECORD"] = ("".join(records) + f"{metadata}/RECORD,,\n").encode()
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return stream.getvalue()


@unittest.skipUnless(UV, "uv is required for mirror integration tests")
class PythonMirrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dazzy-mirror-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        # Isolate the cache and ignore host-specific uv/index/proxy configuration.
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("UV_", "PIP_"))
            and key.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "VIRTUAL_ENV")
        }
        self.env.update(
            UV_CACHE_DIR=str(self.root / "cache"), UV_NO_CONFIG="1",
            UV_PYTHON_DOWNLOADS="never", UV_PYTHON=sys.executable,
            UV_HTTP_RETRIES="0", UV_HTTP_TIMEOUT="5", NO_PROXY="127.0.0.1,localhost",
        )

    def run_uv(self, command, *, success=True):
        result = subprocess.run(
            [UV, *command[1:]], cwd=self.project, env=self.env,
            capture_output=True, timeout=30,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result

    def prepare_fixture(self, *, bad_hash=False):
        wheel = make_test_wheel()
        digest = "0" * 64 if bad_hash else hashlib.sha256(wheel).hexdigest()
        (self.project / "pyproject.toml").write_text(
            '[project]\nname = "mirror-test"\nversion = "0.1.0"\n'
            'requires-python = ">=3.12,<3.13"\n'
            'dependencies = ["dazzy-build-probe==1.0.0"]\n', encoding="utf-8",
        )
        # Original registry matches the project default, but its artifact URL cannot resolve.
        # Successful installation proves that the mirror, NOT the locked URL, supplied the wheel.
        (self.project / "uv.lock").write_text(
            'version = 1\nrevision = 3\nrequires-python = "==3.12.*"\n\n'
            '[[package]]\nname = "dazzy-build-probe"\nversion = "1.0.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            f'wheels = [{{ url = "https://original.invalid/{WHEEL}", '
            f'hash = "sha256:{digest}", size = {len(wheel)} }}]\n\n'
            '[[package]]\nname = "mirror-test"\nversion = "0.1.0"\n'
            'source = { virtual = "." }\ndependencies = [{ name = "dazzy-build-probe" }]\n'
            '[package.metadata]\n'
            'requires-dist = [{ name = "dazzy-build-probe", specifier = "==1.0.0" }]\n',
            encoding="utf-8",
        )
        webroot = self.root / "index"
        simple = webroot / "simple/dazzy-build-probe"
        simple.mkdir(parents=True)
        (simple / "index.html").write_text(f'<a href="/{WHEEL}">{WHEEL}</a>', encoding="utf-8")
        (webroot / WHEEL).write_bytes(wheel)
        requests = []

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                super().do_GET()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=webroot))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop_server)
        commands = python_mirror_commands(
            "https://mirror.example.test/simple", self.project / "requirements.txt",
        )
        # Production validation requires HTTPS; only this local fixture uses loopback HTTP.
        commands[2][commands[2].index("--default-index") + 1] = (
            f"http://127.0.0.1:{server.server_port}/simple"
        )
        return commands, requests

    def test_mirror_downloads_locked_artifact_and_preserves_lockfile(self):
        commands, requests = self.prepare_fixture()
        original = (self.project / "uv.lock").read_bytes()
        for command in commands:
            self.run_uv(command)
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)
        self.assertIn("/simple/dazzy-build-probe/", requests)
        self.assertIn(f"/{WHEEL}", requests)
        result = self.run_uv(["uv", "pip", "freeze", "--python", ".venv", "--offline"])
        self.assertEqual(result.stdout.strip(), b"dazzy-build-probe==1.0.0")

    def test_mirror_rejects_artifact_not_matching_locked_hash(self):
        commands, requests = self.prepare_fixture(bad_hash=True)
        for command in commands[:2]:
            self.run_uv(command)
        result = self.run_uv(commands[2], success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"hash mismatch", result.stderr.lower())
        self.assertIn(f"/{WHEEL}", requests)

    def test_outdated_lock_rejected_before_any_network_request(self):
        commands, requests = self.prepare_fixture()
        path = self.project / "pyproject.toml"
        path.write_text(path.read_text().replace("==1.0.0", "==2.0.0"), encoding="utf-8")
        original = (self.project / "uv.lock").read_bytes()
        result = self.run_uv(commands[0], success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(requests)
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)
        self.assertFalse((self.project / ".venv").exists())

    def test_real_project_exports_exact_runtime_pins_and_hashes_offline(self):
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copyfile(ROOT / name, self.project / name)
        original = (self.project / "uv.lock").read_bytes()
        command = python_mirror_commands(
            "https://mirror.example.test/simple", self.project / "requirements.txt",
        )[0]
        self.run_uv(command)
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)
        lock = tomllib.loads(original.decode())
        locked = {package["name"]: package for package in lock["package"]}
        contents = (self.project / "requirements.txt").read_text()
        self.assertNotIn("files.pythonhosted.org", contents)
        self.assertNotIn("--index-url", contents)
        exported = {}
        for requirement in contents.replace("\\\n", " ").splitlines():
            requirement = requirement.strip()
            if not requirement or requirement.startswith("#"):
                continue
            match = re.match(r"([a-z0-9-]+)==([^ ;]+)", requirement)
            self.assertIsNotNone(match, requirement)
            name, version = match.groups()
            package = locked[name]
            self.assertEqual(version, package["version"])
            expected = {item["hash"] for item in package.get("wheels", [])}
            if "sdist" in package:
                expected.add(package["sdist"]["hash"])
            actual = set(re.findall(r"--hash=(sha256:[0-9a-f]{64})", requirement))
            self.assertTrue(actual)
            self.assertEqual(actual, expected)
            exported[name] = version
        for name in ("dazzy-api", "pytest", "pytest-django", "ruff"):
            self.assertNotIn(name, exported)
        for name in ("celery", "redis", "dg-sdk", "psycopg-binary", "pillow-heif", "django"):
            self.assertIn(name, exported)


if __name__ == "__main__":
    unittest.main()
