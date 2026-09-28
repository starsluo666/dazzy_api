"""Container probes. They do not modify business data or print credentials."""

import json
import os
import socket
import sys
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_api():
    host = os.environ["DJANGO_ALLOWED_HOSTS"].split(",")[0].strip()
    request = Request(
        "http://127.0.0.1:8000/api/v1/health/ready/",
        headers={"Host": host, "X-Forwarded-Proto": "https"},
    )
    with build_opener(NoRedirect).open(request, timeout=10) as response:
        data = json.load(response)["data"]
        if response.status != 200 or data.get("status") != "ready":
            raise RuntimeError("API 尚未就绪")
        if not all(data.get("checks", {}).get(key) is True for key in ("database", "cache")):
            raise RuntimeError("数据库或缓存尚未就绪")


def check_worker():
    from config.celery import app

    name = f"celery@{socket.gethostname()}"
    replies = app.control.inspect(destination=[name], timeout=5).ping() or {}
    if replies.get(name, {}).get("ok") != "pong":
        raise RuntimeError("当前 Worker 未响应")


def check_beat():
    pid = int(Path("/tmp/celerybeat.pid").read_text().strip())
    if pid <= 0:
        raise RuntimeError("Beat PID 无效")
    os.kill(pid, 0)


if __name__ == "__main__":
    try:
        {"api": check_api, "worker": check_worker, "beat": check_beat}[sys.argv[1]]()
    except Exception as exc:
        print(f"健康检查失败：{type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
