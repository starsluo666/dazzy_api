from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from accounts.models import User


class DevelopmentUserAuthentication(BaseAuthentication):
    """Allow a public UUID header only in DEBUG for local App/API integration."""

    header = "HTTP_X_DAZZY_DEMO_USER"

    def authenticate(self, request):
        public_id = request.META.get(self.header)
        if not public_id:
            return None
        if not settings.DEBUG:
            raise AuthenticationFailed("开发用户认证仅可在本地环境使用。")
        configured = settings.DAZZY_DEMO_USER_PUBLIC_ID
        if not configured or public_id != configured:
            raise AuthenticationFailed("开发用户未配置或不匹配。")
        try:
            user = User.objects.get(public_id=public_id, account_status=User.AccountStatus.ACTIVE)
        except (User.DoesNotExist, ValueError) as exc:
            raise AuthenticationFailed("开发用户不存在。") from exc
        return user, None
