from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from providers.models import ProviderProfile

from .models import ProviderOrder
from .serializers import ProviderOrderInputSerializer, ProviderOrderSerializer, quote_payload
from .services import PAYMENT_LOCK_MINUTES, ensure_slot_available


def make_order_no():
    return f"DZY{timezone.now():%Y%m%d%H%M%S%f}"


class ProviderOrderPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ProviderOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"data": quote_payload(serializer.validated_data)})


class ProviderOrderListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        orders = ProviderOrder.objects.filter(customer=request.user).select_related(
            "provider__user", "service__category"
        )[:50]
        return Response({"data": {"items": ProviderOrderSerializer(orders, many=True).data}})

    @transaction.atomic
    def post(self, request):
        serializer = ProviderOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        service = data["service"]
        provider = ProviderProfile.objects.select_for_update().get(pk=service.provider_id)
        ensure_slot_available(provider, data["starts_at"], data["ends_at"])
        quote = data["quote"]
        order = ProviderOrder.objects.create(
            order_no=make_order_no(), customer=request.user, provider=provider, service=service,
            provider_name_snapshot=provider.user.nickname,
            service_name_snapshot=service.category.name,
            billing_type_snapshot=service.billing_type,
            unit_price_amount=service.price_amount,
            starts_at=data["starts_at"], ends_at=data["ends_at"],
            duration_minutes=data["duration_minutes"], meeting_address=data["meeting_address"],
            source_longitude=data.get("longitude"), source_latitude=data.get("latitude"),
            route_distance_km=data.get("route_distance_km"), contact_name=data["contact_name"],
            contact_phone=data["contact_phone"], note=data.get("note", ""),
            service_fee_amount=quote.service_fee_amount,
            transport_fee_amount=quote.transport_fee_amount,
            other_fee_amount=quote.other_fee_amount, discount_amount=quote.discount_amount,
            payable_amount=quote.payable_amount, pricing_snapshot=quote.snapshot,
            payment_expires_at=timezone.now() + timedelta(minutes=PAYMENT_LOCK_MINUTES),
        )
        return Response({"data": ProviderOrderSerializer(order).data}, status=201)


class ProviderOrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, order_no):
        return get_object_or_404(
            ProviderOrder.objects.select_related("provider__user", "service__category"),
            order_no=order_no, customer=request.user,
        )

    def get(self, request, order_no):
        return Response({"data": ProviderOrderSerializer(self.get_object(request, order_no)).data})


class ProviderOrderCancelView(ProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = get_object_or_404(
            ProviderOrder.objects.select_for_update(), order_no=order_no, customer=request.user
        )
        if order.status != ProviderOrder.Status.PENDING_PAYMENT:
            raise ValidationError({"status": "当前阶段暂不支持用户直接取消，请联系客服。"})
        order.status = ProviderOrder.Status.CANCELLED
        order.cancelled_at = timezone.now()
        order.save(update_fields=("status", "cancelled_at", "updated_at"))
        return Response({"data": ProviderOrderSerializer(order).data})


class ProviderOrderSimulatePaymentView(ProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        if not settings.DEBUG:
            raise ValidationError("模拟支付仅在本地环境开放。")
        order = get_object_or_404(
            ProviderOrder.objects.select_for_update(), order_no=order_no, customer=request.user
        )
        if order.status != ProviderOrder.Status.PENDING_PAYMENT:
            raise ValidationError({"status": "订单不在待支付状态。"})
        if order.payment_expires_at <= timezone.now():
            order.status = ProviderOrder.Status.CANCELLED
            order.cancelled_at = timezone.now()
            order.save(update_fields=("status", "cancelled_at", "updated_at"))
            raise ValidationError({"status": "支付已超时，档期已释放。"})
        order.status = ProviderOrder.Status.PENDING_ACCEPTANCE
        order.paid_at = timezone.now()
        order.save(update_fields=("status", "paid_at", "updated_at"))
        return Response({"data": ProviderOrderSerializer(order).data})
