import os

from .base import *  # noqa: F403

DEBUG = True

# 与 production.py 同约定：DJANGO_ALLOWED_HOSTS 逗号分隔，经 .env 注入；
# 本地回环地址始终放行，便于不经配置直接 runserver。
ALLOWED_HOSTS = [
    *filter(None, os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",")),
    "127.0.0.1",
    "localhost",
]

# 本地开发直连的是共享远程库（见 .env POSTGRES_HOST），其 max_connections 仅 100；
# runserver 多线程 + CONN_MAX_AGE 持久连接会长期占住几十个连接位，把生产一起拖垮
# （FATAL: too many clients already）。本地改为一请求一连接，用完即还。
DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
