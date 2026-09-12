from django.db import transaction
from django.db.models import Count
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from orders.models import ProviderOrder
from engagements.models import ProviderFavorite
from config.throttles import SmsSendIpDailyThrottle

from .serializers import (
    ChangePasswordSerializer,
    CloseAccountSerializer,
    LogoutSerializer,
    LogoutOtherSessionsSerializer,
    PasswordLoginSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    SmsCodeRequestSerializer,
    SmsLoginSerializer,
    UserSerializer,
)
from .services import send_sms_code


def auth_payload(user) -> dict:
    refresh = RefreshToken.for_user(user)
    refresh["auth_version"] = user.auth_version
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": UserSerializer(user).data,
    }


class SmsCodeView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, SmsSendIpDailyThrottle]
    throttle_scope = "auth_sms_send"

    def post(self, request):
        serializer = SmsCodeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = send_sms_code(**serializer.validated_data)
        data = {"expires_in": result.expires_in, "retry_after": result.retry_after}
        if result.debug_code:
            data["debug_code"] = result.debug_code
        return Response({"data": data})


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_register"

    @transaction.atomic
    def post(self, request):
        serializer = RegisterSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response({"data": auth_payload(serializer.save())}, status=status.HTTP_201_CREATED)


class PasswordLoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"

    def post(self, request):
        serializer = PasswordLoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response({"data": auth_payload(serializer.validated_data["user"])})


class SmsLoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"

    def post(self, request):
        serializer = SmsLoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response({"data": auth_payload(serializer.validated_data["user"])})


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_password_reset"

    @transaction.atomic
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": {"reset": True}})


class AccountSecurityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        phone = request.user.phone
        return Response(
            {
                "data": {
                    "phone_masked": f"{phone[:3]}****{phone[-4:]}",
                    "password_set": request.user.has_usable_password(),
                    "account_status": request.user.account_status,
                    "account_status_label": request.user.get_account_status_display(),
                }
            }
        )


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_security"

    @transaction.atomic
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response({"data": auth_payload(serializer.save())})


class LogoutOtherSessionsView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_security"

    @transaction.atomic
    def post(self, request):
        serializer = LogoutOtherSessionsSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        return Response({"data": auth_payload(serializer.save())})


class CloseAccountView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_security"

    @transaction.atomic
    def post(self, request):
        serializer = CloseAccountSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": {"closed": True}})


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError:
            return Response({"detail": "刷新令牌无效。"}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"data": UserSerializer(request.user).data})

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": serializer.data})


class CurrentUserOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        orders = ProviderOrder.objects.filter(customer=request.user)
        counts = {
            row["status"]: row["count"]
            for row in orders.values("status").annotate(count=Count("id"))
        }

        def total(*statuses):
            return sum(counts.get(status, 0) for status in statuses)

        return Response(
            {
                "data": {
                    # 钱包和优惠券模型尚未建立，返回 null，避免展示模拟数据。
                    "balance_amount": None,
                    "coupon_count": None,
                    "favorite_count": ProviderFavorite.objects.filter(user=request.user).count(),
                    "order_count": orders.count(),
                    "pending_payment_count": total(ProviderOrder.Status.PENDING_PAYMENT),
                    "pending_service_count": total(
                        ProviderOrder.Status.PENDING_ACCEPTANCE,
                        ProviderOrder.Status.PENDING_SUPPORT,
                        ProviderOrder.Status.PENDING_SERVICE,
                    ),
                    "in_service_count": total(
                        ProviderOrder.Status.DEPARTED,
                        ProviderOrder.Status.IN_SERVICE,
                        ProviderOrder.Status.PENDING_CONFIRMATION,
                    ),
                    "pending_review_count": total(ProviderOrder.Status.PENDING_REVIEW),
                    "after_sales_count": total(
                        ProviderOrder.Status.AFTER_SALES,
                        ProviderOrder.Status.REFUNDED,
                    ),
                }
            }
        )
