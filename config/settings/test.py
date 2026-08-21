from .base import *  # noqa: F403

SECRET_KEY = "test-only-secret-key-at-least-32-bytes-long"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
