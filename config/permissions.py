from django.conf import settings
from rest_framework.permissions import BasePermission


class DebugOnlyPermission(BasePermission):
    """Allow development-only diagnostics only while Django DEBUG is enabled."""

    def has_permission(self, request, view):
        return settings.DEBUG
