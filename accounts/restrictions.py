from rest_framework.exceptions import PermissionDenied

from .models import UserFunctionRestriction


def ensure_function_allowed(user, function):
    """Read current restrictions; mutation callers also hold the user-row lock."""
    if UserFunctionRestriction.objects.filter(
        user_id=user.pk, function=function, is_active=True,
    ).exists():
        label = UserFunctionRestriction.Function(function).label
        raise PermissionDenied(f"当前账号已被限制{label}，如有疑问请联系客服。")
