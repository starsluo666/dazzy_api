from .base import *  # noqa: F403

SECRET_KEY = "test-only-secret-key"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
