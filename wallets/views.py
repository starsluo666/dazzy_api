from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.wechat_oauth import (
    WechatOAuthConfigurationError,
    build_payment_authorization,
    get_official_account_openid,
)

from .models import RechargeCampaign, WalletRechargeOrder
from .serializers import RechargeOrderInputSerializer, RechargePaymentSessionInputSerializer
from .services import (
    confirm_recharge_payment,
    create_recharge_huifu_payment_session,
    create_recharge_order,
    get_or_create_wallet,
    recharge_order_payload,
)


def ledger_payload(entry):
    return {
        "public_id": str(entry.public_id),
        "entry_type": entry.entry_type,
        "entry_type_label": entry.get_entry_type_display(),
        "available_delta": entry.available_delta,
        "frozen_delta": entry.frozen_delta,
        "available_balance_after": entry.available_balance_after,
        "description": entry.description,
        "reference_type": entry.reference_type,
        "reference_no": entry.reference_no,
        "created_at": entry.created_at,
    }


class CurrentWalletView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet = get_or_create_wallet(request.user.pk)
        entries = wallet.ledger_entries.all()[:100]
        return Response(
            {
                "data": {
                    "available_balance": wallet.available_balance,
                    "frozen_balance": wallet.frozen_balance,
                    "total_balance": wallet.available_balance + wallet.frozen_balance,
                    "ledger_entries": [ledger_payload(item) for item in entries],
                }
            }
        )


class RechargeCampaignView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        campaign = RechargeCampaign.current()
        tiers = campaign.discount_tiers.all()
        return Response(
            {
                "data": {
                    "is_enabled": campaign.is_enabled,
                    "unit_face_amount": campaign.unit_face_amount,
                    "max_quantity_per_order": campaign.max_quantity_per_order,
                    "rules_text": campaign.rules_text,
                    "tiers": [
                        {
                            "min_quantity": item.min_quantity,
                            "discount_rate_bps": item.discount_rate_bps,
                        }
                        for item in tiers
                    ],
                }
            }
        )


class RechargeOrderListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        orders = WalletRechargeOrder.objects.filter(user=request.user)[:50]
        return Response({"data": {"items": [recharge_order_payload(item) for item in orders]}})

    def post(self, request):
        serializer = RechargeOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = create_recharge_order(
            user_id=request.user.pk, quantity=serializer.validated_data["quantity"]
        )
        return Response({"data": recharge_order_payload(order)}, status=201)


class RechargeOrderAuthorizationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, order_no):
        order = get_object_or_404(
            WalletRechargeOrder,
            order_no=order_no,
            user=request.user,
            status=WalletRechargeOrder.Status.PENDING_PAYMENT,
        )
        return Response(
            {
                "data": build_payment_authorization(
                    user_id=request.user.pk,
                    order_no=order.order_no,
                    payment_kind="wallet_recharge",
                )
            }
        )


class RechargeOrderPaymentSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, order_no):
        serializer = RechargePaymentSessionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment_scene = serializer.validated_data["payment_scene"]
        sub_openid = ""
        if payment_scene == "official_account":
            app_id = settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip()
            if not app_id:
                raise WechatOAuthConfigurationError()
            sub_openid = get_official_account_openid(user_id=request.user.pk, app_id=app_id)
            if not sub_openid:
                raise ValidationError({"authorization": "请先在微信服务号内完成网页授权。"})
        order, created = create_recharge_huifu_payment_session(
            order_no=order_no,
            user_id=request.user.pk,
            payment_scene=payment_scene,
            sub_openid=sub_openid,
        )
        return Response(
            {
                "data": {
                    "invoke_type": "WECHAT_JSAPI" if order.trade_type == "T_JSAPI" else "WECHAT_APP",
                    "pay_info": order.payment_invoke_payload,
                    "order": recharge_order_payload(order),
                }
            },
            status=201 if created else 200,
        )


class RechargeOrderPaymentStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, order_no):
        order, changed = confirm_recharge_payment(order_no=order_no, user_id=request.user.pk)
        return Response({"data": {"order": recharge_order_payload(order), "changed": changed}})
