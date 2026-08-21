from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    CurrentUserView,
    CurrentUserOverviewView,
    LogoutView,
    PasswordLoginView,
    RegisterView,
    ResetPasswordView,
    SmsCodeView,
    SmsLoginView,
)

urlpatterns = [
    path("auth/sms-codes/", SmsCodeView.as_view(), name="auth-sms-code"),
    path("auth/register/", RegisterView.as_view(), name="auth-register"),
    path("auth/login/password/", PasswordLoginView.as_view(), name="auth-password-login"),
    path("auth/login/sms/", SmsLoginView.as_view(), name="auth-sms-login"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="auth-token-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/password/reset/", ResetPasswordView.as_view(), name="auth-password-reset"),
    path("users/me/", CurrentUserView.as_view(), name="current-user"),
    path("users/me/overview/", CurrentUserOverviewView.as_view(), name="current-user-overview"),
]
