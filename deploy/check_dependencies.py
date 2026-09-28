"""Check PostGIS, Redis cache and the broker before a release touches live services."""

import sys
import uuid

import django


def main():
    django.setup()
    from django.core.cache import cache
    from django.db import connection

    from config.celery import app

    with connection.cursor() as cursor:
        cursor.execute("SELECT PostGIS_Version()")
        cursor.fetchone()
    key = f"deploy-probe:{uuid.uuid4().hex}"
    try:
        cache.set(key, "ok", timeout=30)
        if cache.get(key) != "ok":
            raise RuntimeError("Redis 缓存读写检查失败")
    finally:
        cache.delete(key)
    with app.connection_for_write(connect_timeout=5) as broker:
        broker.ensure_connection(max_retries=0)
    print("[预检] PostgreSQL/PostGIS、Redis 缓存和 Celery 消息通道连接正常")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"依赖连接检查失败：{type(exc).__name__}，请检查业务环境配置和 Docker 网络。",
            file=sys.stderr,
        )
        sys.exit(1)
