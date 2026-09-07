from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from accounts.serializers import PasswordLoginSerializer

from .access import resolve_admin_access


def _set_refresh_cookie(response, refresh_token: str) -> None:
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response.set_cookie(
        key=settings.ADMIN_REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.ADMIN_REFRESH_COOKIE_MAX_AGE,
        path=settings.ADMIN_REFRESH_COOKIE_PATH,
        domain=settings.ADMIN_REFRESH_COOKIE_DOMAIN or None,
        secure=settings.ADMIN_REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.ADMIN_REFRESH_COOKIE_SAMESITE,
    )


def _clear_refresh_cookie(response) -> None:
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response.delete_cookie(
        key=settings.ADMIN_REFRESH_COOKIE_NAME,
        path=settings.ADMIN_REFRESH_COOKIE_PATH,
        domain=settings.ADMIN_REFRESH_COOKIE_DOMAIN or None,
        samesite=settings.ADMIN_REFRESH_COOKIE_SAMESITE,
    )


def _token_pair(user) -> tuple[str, str]:
    refresh = RefreshToken.for_user(user)
    refresh["auth_version"] = user.auth_version
    return str(refresh.access_token), str(refresh)


def _expired_response(detail="管理端登录已过期，请重新登录。", status_code=status.HTTP_401_UNAUTHORIZED):
    response = Response({"detail": detail}, status=status_code)
    _clear_refresh_cookie(response)
    return response


class AdminPasswordLoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"

    def post(self, request):
        serializer = PasswordLoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        resolve_admin_access(user)
        access_token, refresh_token = _token_pair(user)
        response = Response({"data": {"access": access_token}})
        _set_refresh_cookie(response, refresh_token)
        return response


class AdminTokenRefreshView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin_auth_refresh"

    def post(self, request):
        raw_token = request.COOKIES.get(settings.ADMIN_REFRESH_COOKIE_NAME)
        if not raw_token:
            return _expired_response()
        try:
            refresh = RefreshToken(raw_token)
            user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)
            if user_id is None:
                raise TokenError("Token contained no recognizable user identification")
            user = User.objects.get(**{api_settings.USER_ID_FIELD: user_id})
            if (
                not user.is_active
                or user.account_status != User.AccountStatus.ACTIVE
                or refresh.get("auth_version") != user.auth_version
            ):
                raise AuthenticationFailed("登录状态已失效，请重新登录。")
            resolve_admin_access(user)
            serializer = TokenRefreshSerializer(data={"refresh": raw_token})
            serializer.is_valid(raise_exception=True)
        except PermissionDenied as exc:
            return _expired_response(str(exc.detail), status.HTTP_403_FORBIDDEN)
        except (AuthenticationFailed, TokenError, User.DoesNotExist):
            return _expired_response()

        access_token = serializer.validated_data["access"]
        rotated_refresh = serializer.validated_data.get("refresh", raw_token)
        response = Response({"data": {"access": access_token}})
        _set_refresh_cookie(response, rotated_refresh)
        return response


class AdminLogoutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        raw_token = request.COOKIES.get(settings.ADMIN_REFRESH_COOKIE_NAME)
        if raw_token:
            try:
                RefreshToken(raw_token).blacklist()
            except TokenError:
                pass
        response = Response(status=status.HTTP_204_NO_CONTENT)
        _clear_refresh_cookie(response)
        return response
