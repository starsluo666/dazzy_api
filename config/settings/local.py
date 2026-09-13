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
