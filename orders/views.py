from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backoffice.operation_settings import platform_operation_rules
from mediafiles.models import MediaAsset
from providers.models import ProviderProfile
from providers.presence import operation_rules
from taskcenter.services import (
    cancel_provider_acceptance_timeout,
    cancel_provider_order_payment_expiry,
    mark_provider_acceptance_expired,
    mark_provider_order_payment_expired,
    register_provider_acceptance_timeout,
    register_provider_order_payment_expiry,
)

from .models import ProviderOrder
from .serializers import (
    ProviderOrderInputSerializer,
    ProviderOrderArrivalEvidenceInputSerializer,
    ProviderOrderManageQuerySerializer,
    ProviderOrderManageSerializer,
    ProviderOrderRejectInputSerializer,
    ProviderOrderSerializer,
    quote_payload,
)
from .services import ensure_slot_available, expire_pending_orders


def make_order_no():
    return f"DZY{timezone.now():%Y%m%d%H%M%S%f}"


def current_approved_provider(request):
    provider = get_object_or_404(ProviderProfile, user=request.user)
    if provider.status != ProviderProfile.Status.APPROVED:
        raise PermissionDenied("仅审核通过的达人可以管理订单。")
    return provider


class ProviderOrderPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ProviderOrderInputSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response({"data": quote_payload(serializer.validated_data)})


class ProviderOrderListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        customer_orders = ProviderOrder.objects.filter(customer=request.user)
        expire_pending_orders(customer_orders)
        orders = customer_orders.select_related(
            "provider__user", "service__category", "arrival_photo"
        )[:50]
        return Response({"data": {"items": ProviderOrderSerializer(orders, many=True).data}})

    @transaction.atomic
    def post(self, request):
        serializer = ProviderOrderInputSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        service = data["service"]
        provider = ProviderProfile.objects.select_for_update().get(pk=service.provider_id)
        ensure_slot_available(provider, data["starts_at"], data["ends_at"])
        quote = data["quote"]
        order = ProviderOrder.objects.create(
            order_no=make_order_no(),
            customer=request.user,
            provider=provider,
            service=service,
            provider_name_snapshot=provider.user.nickname,
            service_name_snapshot=service.category.name,
            billing_type_snapshot=service.billing_type,
            unit_price_amount=service.price_amount,
            starts_at=data["starts_at"],
            ends_at=data["ends_at"],
            duration_minutes=data["duration_minutes"],
            meeting_location_name=data["meeting_location_name"],
            meeting_address=data["meeting_address"],
            source_longitude=data.get("longitude"),
            source_latitude=data.get("latitude"),
            route_distance_km=data["route"].distance_km,
            map_source="tencent",
            contact_name=data["contact_name"],
            contact_gender=data["contact_gender"],
            contact_phone=data["contact_phone"],
            note=data.get("note", ""),
            service_fee_amount=quote.service_fee_amount,
            transport_fee_amount=quote.transport_fee_amount,
            other_fee_amount=quote.other_fee_amount,
            discount_amount=quote.discount_amount,
            payable_amount=quote.payable_amount,
            pricing_snapshot=quote.snapshot,
            payment_expires_at=timezone.now()
            + timedelta(
                minutes=platform_operation_rules()["provider_order_payment_timeout_minutes"]
            ),
        )
        register_provider_order_payment_expiry(order)
        return Response({"data": ProviderOrderSerializer(order).data}, status=201)


class ProviderOrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, order_no):
        expire_pending_orders(
            ProviderOrder.objects.filter(customer=request.user, order_no=order_no)
        )
        return get_object_or_404(
            ProviderOrder.objects.select_related(
                "provider__user", "service__category", "arrival_photo"
            ),
            order_no=order_no,
            customer=request.user,
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
        cancel_provider_order_payment_expiry(order_no, "customer_cancelled")
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
            mark_provider_order_payment_expired(order_no, source="payment_guard")
            return Response({"error": {"status": "支付已超时，档期已释放。"}}, status=409)
        paid_at = timezone.now()
        acceptance_timeout = operation_rules()["acceptance_timeout_minutes"]
        order.status = ProviderOrder.Status.PENDING_ACCEPTANCE
        order.paid_at = paid_at
        order.acceptance_expires_at = paid_at + timedelta(minutes=acceptance_timeout)
        order.save(update_fields=("status", "paid_at", "acceptance_expires_at", "updated_at"))

        cancel_provider_order_payment_expiry(order_no, "payment_succeeded")
        register_provider_acceptance_timeout(order)
        return Response({"data": ProviderOrderSerializer(order).data})


class CurrentProviderOrderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider = current_approved_provider(request)
        query = ProviderOrderManageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        orders = ProviderOrder.objects.filter(
            provider=provider,
            paid_at__isnull=False,
        ).select_related("customer", "provider__user", "service__category", "arrival_photo")
        if order_status := query.validated_data.get("status"):
            orders = orders.filter(status=order_status)
        return Response(
            {"data": {"items": ProviderOrderManageSerializer(orders[:50], many=True).data}}
        )


class CurrentProviderOrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, order_no, *, for_update=False):
        provider = current_approved_provider(request)
        queryset = ProviderOrder.objects
        if for_update:
            queryset = queryset.select_for_update(of=("self",))
        return get_object_or_404(
            queryset.select_related(
                "customer", "provider__user", "service__category", "arrival_photo"
            ),
            provider=provider,
            paid_at__isnull=False,
            order_no=order_no,
        )

    def get(self, request, order_no):
        order = self.get_object(request, order_no)
        return Response({"data": ProviderOrderManageSerializer(order).data})


class CurrentProviderOrderAcceptView(CurrentProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = self.get_object(request, order_no, for_update=True)
        if order.status == ProviderOrder.Status.PENDING_SERVICE and order.accepted_at:
            return Response({"data": ProviderOrderManageSerializer(order).data})
        if order.status != ProviderOrder.Status.PENDING_ACCEPTANCE:
            raise ValidationError({"status": "订单不在待接单状态。"})
        timeout = operation_rules()["acceptance_timeout_minutes"]
        deadline = order.acceptance_expires_at or (
            order.paid_at + timedelta(minutes=timeout) if order.paid_at else None
        )
        if not deadline or deadline <= timezone.now():
            order.status = ProviderOrder.Status.PENDING_SUPPORT
            order.save(update_fields=("status", "updated_at"))
            mark_provider_acceptance_expired(order_no, source="provider_action_guard")
            return Response(
                {"error": {"status": f"订单已超过{timeout}分钟接单时限，请联系客服。"}},
                status=409,
            )
        order.status = ProviderOrder.Status.PENDING_SERVICE
        order.accepted_at = timezone.now()
        order.save(update_fields=("status", "accepted_at", "updated_at"))
        cancel_provider_acceptance_timeout(order_no, "provider_accepted")
        return Response({"data": ProviderOrderManageSerializer(order).data})


class CurrentProviderOrderRejectView(CurrentProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = self.get_object(request, order_no, for_update=True)
        if order.status == ProviderOrder.Status.PENDING_SUPPORT and order.provider_rejected_at:
            return Response({"data": ProviderOrderManageSerializer(order).data})
        if order.status != ProviderOrder.Status.PENDING_ACCEPTANCE:
            raise ValidationError({"status": "订单不在待接单状态。"})
        timeout = operation_rules()["acceptance_timeout_minutes"]
        deadline = order.acceptance_expires_at or (
            order.paid_at + timedelta(minutes=timeout) if order.paid_at else None
        )
        if not deadline or deadline <= timezone.now():
            order.status = ProviderOrder.Status.PENDING_SUPPORT
            order.save(update_fields=("status", "updated_at"))
            mark_provider_acceptance_expired(order_no, source="provider_action_guard")
            return Response(
                {"error": {"status": f"订单已超过{timeout}分钟接单时限，请联系客服。"}},
                status=409,
            )
        serializer = ProviderOrderRejectInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order.status = ProviderOrder.Status.PENDING_SUPPORT
        order.provider_rejected_at = timezone.now()
        order.provider_rejection_reason = serializer.validated_data["reason"]
        order.save(
            update_fields=(
                "status",
                "provider_rejected_at",
                "provider_rejection_reason",
                "updated_at",
            )
        )
        cancel_provider_acceptance_timeout(order_no, "provider_rejected")
        return Response({"data": ProviderOrderManageSerializer(order).data})


class CurrentProviderOrderDepartView(CurrentProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = self.get_object(request, order_no, for_update=True)
        if order.status == ProviderOrder.Status.DEPARTED and order.departed_at:
            return Response({"data": ProviderOrderManageSerializer(order).data})
        if order.status != ProviderOrder.Status.PENDING_SERVICE:
            raise ValidationError({"status": "订单不在待服务状态。"})
        order.status = ProviderOrder.Status.DEPARTED
        order.departed_at = timezone.now()
        order.save(update_fields=("status", "departed_at", "updated_at"))
        return Response({"data": ProviderOrderManageSerializer(order).data})


class CurrentProviderOrderArrivalEvidenceView(CurrentProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = self.get_object(request, order_no, for_update=True)
        if order.status != ProviderOrder.Status.DEPARTED:
            raise ValidationError({"status": "仅已出发订单可上传集合照。"})
        serializer = ProviderOrderArrivalEvidenceInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            photo = MediaAsset.objects.get(
                pk=data["photo_id"],
                owner=request.user,
                category=MediaAsset.Category.ORDER_EVIDENCE,
                status=MediaAsset.Status.UPLOADED,
            )
        except MediaAsset.DoesNotExist as exc:
            raise ValidationError({"photo_id": "集合照不存在或不可用。"}) from exc
        if ProviderOrder.objects.exclude(pk=order.pk).filter(arrival_photo=photo).exists():
            raise ValidationError({"photo_id": "该照片已绑定其他订单。"})
        order.arrival_photo = photo
        order.arrival_photo_uploaded_at = timezone.now()
        order.arrival_longitude = data["longitude"]
        order.arrival_latitude = data["latitude"]
        order.arrival_location_accuracy_m = data.get("accuracy_m")
        order.save(
            update_fields=(
                "arrival_photo",
                "arrival_photo_uploaded_at",
                "arrival_longitude",
                "arrival_latitude",
                "arrival_location_accuracy_m",
                "updated_at",
            )
        )
        return Response({"data": ProviderOrderManageSerializer(order).data})


class CurrentProviderOrderStartView(CurrentProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = self.get_object(request, order_no, for_update=True)
        if order.status == ProviderOrder.Status.IN_SERVICE and order.service_started_at:
            return Response({"data": ProviderOrderManageSerializer(order).data})
        if order.status != ProviderOrder.Status.DEPARTED:
            raise ValidationError({"status": "订单不在已出发状态。"})
        if not order.arrival_photo_id:
            raise ValidationError({"arrival_photo": "请先上传集合地点照片。"})
        order.status = ProviderOrder.Status.IN_SERVICE
        order.service_started_at = timezone.now()
        order.save(update_fields=("status", "service_started_at", "updated_at"))
        return Response({"data": ProviderOrderManageSerializer(order).data})


class CurrentProviderOrderCompleteView(CurrentProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = self.get_object(request, order_no, for_update=True)
        if (
            order.status == ProviderOrder.Status.PENDING_CONFIRMATION
            and order.completion_submitted_at
        ):
            return Response({"data": ProviderOrderManageSerializer(order).data})
        if order.status != ProviderOrder.Status.IN_SERVICE:
            raise ValidationError({"status": "订单不在服务中状态。"})
        order.status = ProviderOrder.Status.PENDING_CONFIRMATION
        order.completion_submitted_at = timezone.now()
        order.save(update_fields=("status", "completion_submitted_at", "updated_at"))
        return Response({"data": ProviderOrderManageSerializer(order).data})


class ProviderOrderConfirmCompletionView(ProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = get_object_or_404(
            ProviderOrder.objects.select_related(
                "provider__user", "service__category", "arrival_photo"
            ).select_for_update(of=("self",)),
            order_no=order_no,
            customer=request.user,
        )
        if order.status == ProviderOrder.Status.PENDING_REVIEW and order.customer_confirmed_at:
            return Response({"data": ProviderOrderSerializer(order).data})
        if order.status != ProviderOrder.Status.PENDING_CONFIRMATION:
            raise ValidationError({"status": "订单不在待确认状态。"})
        order.status = ProviderOrder.Status.PENDING_REVIEW
        order.customer_confirmed_at = timezone.now()
        order.save(update_fields=("status", "customer_confirmed_at", "updated_at"))
        return Response({"data": ProviderOrderSerializer(order).data})
