from .base import *  # noqa: F403

SECRET_KEY = "test-only-secret-key-at-least-32-bytes-long"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
# Tests must opt in explicitly with a mocked SMS transport, never inherit a live gateway.
TENCENT_SMS_ENABLED = False
CONTENT_REVIEW_ENABLED = False
TENCENT_CONTENT_REVIEW_ENABLED = False
