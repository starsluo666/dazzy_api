"""Build-only dependency setup. Never load Django settings or business secrets."""

from contextlib import ExitStack
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit


SYSTEM_PACKAGES = (
    "libgdal32", "libgeos-c1v5", "libproj25", "ca-certificates",
    "fonts-wqy-microhei", "ffmpeg",
)


def mirror_url(value):
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError("Invalid package mirror URL.") from None
    if (
        parts.scheme not in ("http", "https") or not parts.hostname
        or parts.username is not None or parts.password is not None
        or "?" in value or "#" in value or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ValueError("Package mirror must be an HTTP(S) URL without credentials or query/fragment.")
    # Validate the port too; never print the input, which could contain credentials.
    try:
        parts.port
    except ValueError:
        raise ValueError("Invalid package mirror port.") from None
    return value.rstrip("/")


def configure_apt(env, root=Path("/etc/apt")):
    main = mirror_url(env.get("DEBIAN_MIRROR", "https://deb.debian.org/debian"))
    security = mirror_url(env.get("DEBIAN_SECURITY_MIRROR", "https://deb.debian.org/debian-security"))
    sources = root / "sources.list.d/debian.sources"
    original = sources.read_text(encoding="utf-8")
    replacements = {
        f"{scheme}://{host}/{path}": replacement
        for scheme in ("http", "https")
        for host, path, replacement in (
            ("deb.debian.org", "debian", main),
            ("deb.debian.org", "debian-security", security),
            ("security.debian.org", "debian-security", security),
        )
    }
    seen = set()

    def replace_uri(match):
        uris = match.group(1).split()
        if any(uri not in replacements for uri in uris):
            raise ValueError("Unexpected Debian base-image sources; review before replacing mirrors.")
        for uri in uris:
            seen.add("security" if uri.endswith("/debian-security") else "main")
        return "URIs: " + " ".join(replacements[uri] for uri in uris)

    rewritten = re.sub(r"^URIs:[ \t]*(.+)$", replace_uri, original, flags=re.MULTILINE)
    if seen != {"main", "security"}:
        raise ValueError("Both Debian main and security sources are required.")
    # Keep suites, components and Debian Signed-By keys unchanged.
    sources.write_text(rewritten, encoding="utf-8")
    config_dir = root / "apt.conf.d"
    (config_dir / "docker-clean").unlink(missing_ok=True)
    (config_dir / "99dazzy-build").write_text(
        'APT::Keep-Downloaded-Packages "true";\n'
        'Binary::apt::APT::Keep-Downloaded-Packages "true";\n'
        'Acquire::Retries "3";\n'
        'Acquire::http::Timeout "30";\n'
        'Acquire::https::Timeout "30";\n'
        'Acquire::Languages "none";\n',
        encoding="utf-8",
    )
    print(f"[dazzy-build] Debian: {main}; security: {security}", flush=True)


def python_mirror_commands(index, requirements):
    """Export pins/hashes, not artifact URLs; only the install step changes index."""
    index = mirror_url(index)
    if urlsplit(index).scheme != "https":
        raise ValueError("PYTHON_PACKAGE_INDEX must use HTTPS.")
    return [
        ["uv", "export", "--locked", "--offline", "--no-dev", "--no-emit-project",
         "--format", "requirements.txt", "--output-file", str(requirements), "--quiet"],
        ["uv", "venv", "--python", sys.executable, ".venv"],
        ["uv", "pip", "sync", "--python", ".venv", "--require-hashes",
         "--default-index", index, str(requirements)],
    ]


def install(kind, *, env=None, apt_root=Path("/etc/apt")):
    env = dict(os.environ if env is None else env)
    duration = env.get("BUILD_DEPENDENCY_TIMEOUT", "1200")
    if not re.fullmatch(r"[0-9]{2,4}", duration) or not 60 <= int(duration) <= 7200:
        raise ValueError("BUILD_DEPENDENCY_TIMEOUT must be 60..7200 seconds.")
    with ExitStack() as cleanup:
        if kind == "apt":
            configure_apt(env, apt_root)
            env["DEBIAN_FRONTEND"] = "noninteractive"
            # Reject partial index-update failures rather than using stale cached lists.
            command = ["sh", "-ec", "apt-get update --error-on=any && " + shlex.join(
                ["apt-get", "install", "-y", "--no-install-recommends", *SYSTEM_PACKAGES],
            )]
        elif kind == "python":
            env.update(UV_HTTP_CONNECT_TIMEOUT="10", UV_HTTP_TIMEOUT="30", UV_HTTP_RETRIES="3")
            index = env.get("PYTHON_PACKAGE_INDEX", "")
            if index:
                temporary = cleanup.enter_context(TemporaryDirectory(prefix="dazzy-build-"))
                commands = python_mirror_commands(index, Path(temporary) / "requirements.txt")
                # One timeout covers export, environment creation AND installation. An export
                # failure must not fall through to installing an empty/unlocked requirements file.
                command = ["sh", "-ec", " && ".join(shlex.join(step) for step in commands)]
                print(f"[dazzy-build] Python mirror: {mirror_url(index)} (locked pins + hashes)",
                      flush=True)
            else:
                command = ["uv", "sync", "--locked", "--no-dev", "--no-install-project"]
        else:
            raise ValueError("Expected dependency kind: apt or python.")
        print(f"[dazzy-build] Installing {kind} dependencies; step timeout: {duration}s", flush=True)
        result = subprocess.run(
            ["timeout", "--kill-after=30s", f"{int(duration)}s", *command], env=env, check=False,
        )
    if result.returncode:
        reason = "timed out" if result.returncode in (124, 137) else "failed"
        print(
            f"[dazzy-build] {kind} dependency installation {reason} (exit {result.returncode}). "
            "Check the download source/network; cache is retained for retry. "
            + ("For slow PyPI downloads, set DAZZY_PYTHON_PACKAGE_INDEX in .env.docker. "
               if kind == "python" else "") +
            "If the connection is healthy but slow, increase DAZZY_BUILD_DEPENDENCY_TIMEOUT "
            "in .env.docker (max 7200s).",
            file=sys.stderr,
        )
    return result.returncode


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Usage: build_dependencies.py apt|python")
        raise SystemExit(install(sys.argv[1]))
    except ValueError as exc:
        print(f"[dazzy-build] {exc}", file=sys.stderr)
        raise SystemExit(2) from None
