from django.apps import AppConfig


class GrowthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "growth"
    verbose_name = "拉新活动"

    def ready(self):
        from . import signals  # noqa: F401
