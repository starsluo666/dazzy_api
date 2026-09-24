from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    AccountSecurityView,
    ChangePhoneCodeView,
    ChangePhoneView,
    CloseAccountView,
    ChangePasswordView,
    CurrentUserView,
    CurrentUserOverviewView,
    LogoutView,
    LogoutOtherSessionsView,
    PasswordLoginView,
    RegisterView,
    ResetPasswordView,
    SmsCodeView,
    SmsLoginView,
    WechatMiniProgramLoginView,
)

urlpatterns = [
    path("auth/sms-codes/", SmsCodeView.as_view(), name="auth-sms-code"),
    path("auth/register/", RegisterView.as_view(), name="auth-register"),
    path("auth/login/password/", PasswordLoginView.as_view(), name="auth-password-login"),
    path("auth/login/sms/", SmsLoginView.as_view(), name="auth-sms-login"),
    path(
        "auth/login/wechat-mini-program/",
        WechatMiniProgramLoginView.as_view(),
        name="auth-wechat-mini-program-login",
    ),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="auth-token-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/security/", AccountSecurityView.as_view(), name="auth-security"),
    path("auth/account/close/", CloseAccountView.as_view(), name="auth-account-close"),
    path("auth/password/change/", ChangePasswordView.as_view(), name="auth-password-change"),
    path(
        "auth/phone/change/code/",
        ChangePhoneCodeView.as_view(),
        name="auth-phone-change-code",
    ),
    path("auth/phone/change/", ChangePhoneView.as_view(), name="auth-phone-change"),
    path(
        "auth/sessions/logout-others/",
        LogoutOtherSessionsView.as_view(),
        name="auth-logout-other-sessions",
    ),
    path("auth/password/reset/", ResetPasswordView.as_view(), name="auth-password-reset"),
    path("users/me/", CurrentUserView.as_view(), name="current-user"),
    path("users/me/overview/", CurrentUserOverviewView.as_view(), name="current-user-overview"),
]
