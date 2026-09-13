import os

from .base import *  # noqa: F403

DEBUG = True

# 本地联调可能经 hosts/代理用正式域名访问；额外域名用 LOCAL_EXTRA_HOSTS 逗号分隔配置。
ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "api.ledaban.cn",
    "ledaban.cn",
    *filter(None, os.getenv("LOCAL_EXTRA_HOSTS", "").split(",")),
]
