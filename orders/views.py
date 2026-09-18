from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from backoffice.access import client_ip
from backoffice.models import AdminAuditLog, ProviderOrderAfterSalesCase
from backoffice.operation_settings import platform_operation_rules
from mediafiles.models import MediaAsset
from notifications.models import UserNotification
from notifications.services import create_order_notification
from providers.availability import ensure_booking_within_schedule
from providers.models import ProviderProfile
from providers.presence import operation_rules
from taskcenter.services import (
    cancel_provider_acceptance_timeout,
    cancel_provider_order_confirmation_timeout,
    cancel_provider_order_payment_expiry,
    mark_provider_acceptance_expired,
    register_provider_rejection_support_timeout,
    register_provider_order_confirmation_timeout,
    register_provider_order_payment_expiry,
)

from .models import ProviderOrder, ProviderOrderPaymentOrder, ProviderOrderReview
from .payment_gateway import get_provider_order_payment_gateway
from .serializers import (
    ProviderOrderAfterSalesInputSerializer,
    ProviderOrderAfterSalesSerializer,
    ProviderOrderInputSerializer,
    ProviderOrderPaymentSessionInputSerializer,
    ProviderOrderArrivalEvidenceInputSerializer,
    ProviderOrderManageQuerySerializer,
    ProviderOrderManageSerializer,
    MyProviderOrderReviewSerializer,
    ProviderOrderReviewListQuerySerializer,
    ProviderOrderReviewInputSerializer,
    ProviderOrderSerializer,
    quote_payload,
)
from .services import (
    PROVIDER_REJECTION_SUPPORT_TIMEOUT,
    apply_provider_order_payment_success,
    confirm_provider_order_huifu_payment_status,
    create_customer_provider_order_after_sales_case,
    create_provider_order_huifu_payment_session,
    create_provider_order_payment_order,
    ensure_provider_order_settlement,
    ensure_slot_available,
    refresh_provider_review_metrics,
    process_huifu_payment_notification,
)
from .wechat_oauth import (
    WechatOAuthConfigurationError,
    build_payment_authorization,
    complete_payment_authorization,
    get_official_account_openid,
)


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
        orders = customer_orders.select_related(
            "provider__user", "service__category", "arrival_photo", "review__customer",
            "payment_order", "settlement",
        ).prefetch_related(
            "review__images", "refund_orders", "after_sales_cases"
        )[:50]
        return Response({"data": {"items": ProviderOrderSerializer(orders, many=True).data}})

    def post(self, request):
        serializer = ProviderOrderInputSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        service = data["service"]
        # The route lookup in serializer validation is network I/O and must stay
        # outside the database transaction. Recheck mutable scheduling state only
        # after taking the provider lock.
        with transaction.atomic():
            provider = (
                ProviderProfile.objects.select_for_update()
                .select_related("user")
                .get(pk=service.provider_id)
            )
            ensure_booking_within_schedule(
                provider, data["starts_at"], data["ends_at"]
            )
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
                    minutes=platform_operation_rules()[
                        "provider_order_payment_timeout_minutes"
                    ]
                ),
            )
            create_provider_order_payment_order(order)
            register_provider_order_payment_expiry(order)
        return Response({"data": ProviderOrderSerializer(order).data}, status=201)


class ProviderOrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, order_no):
        return get_object_or_404(
            ProviderOrder.objects.select_related(
                "provider__user", "service__category", "arrival_photo", "review__customer",
                "payment_order", "settlement",
            ).prefetch_related("review__images", "refund_orders", "after_sales_cases"),
            order_no=order_no,
            customer=request.user,
        )

    def get(self, request, order_no):
        return Response({"data": ProviderOrderSerializer(self.get_object(request, order_no)).data})


class ProviderOrderAfterSalesView(APIView):
    permission_classes = [IsAuthenticated]

    def get_order(self, request, order_no):
        return get_object_or_404(
            ProviderOrder.objects.only("id", "order_no"),
            order_no=order_no,
            customer=request.user,
        )

    def get(self, request, order_no):
        order = self.get_order(request, order_no)
        cases = ProviderOrderAfterSalesCase.objects.filter(order=order).select_related(
            "order"
        ).prefetch_related("order__refund_orders")
        return Response(
            {"data": {"items": ProviderOrderAfterSalesSerializer(cases, many=True).data}}
        )

    def post(self, request, order_no):
        self.get_order(request, order_no)
        serializer = ProviderOrderAfterSalesInputSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        evidence_assets = serializer.validated_data.get("evidence_asset_ids", [])
        case, created = create_customer_provider_order_after_sales_case(
            order_no=order_no,
            customer=request.user,
            case_type=serializer.validated_data["case_type"],
            requested_amount=serializer.validated_data["requested_amount"],
            reason=serializer.validated_data["reason"],
            evidence_object_keys=[asset.object_key for asset in evidence_assets],
        )
        case = ProviderOrderAfterSalesCase.objects.select_related("order").prefetch_related(
            "order__refund_orders"
        ).get(pk=case.pk)
        return Response(
            {"data": ProviderOrderAfterSalesSerializer(case).data},
            status=201 if created else 200,
        )


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
        payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
        if payment and payment.status == ProviderOrderPaymentOrder.Status.PENDING_PAYMENT:
            payment.status = ProviderOrderPaymentOrder.Status.CLOSED
            payment.closed_at = order.cancelled_at
            payment.save(update_fields=("status", "closed_at", "updated_at"))
        cancel_provider_order_payment_expiry(order_no, "customer_cancelled")
        return Response({"data": ProviderOrderSerializer(order).data})


class ProviderOrderSimulatePaymentView(ProviderOrderDetailView):
    def post(self, request, order_no):
        if not settings.DEBUG:
            raise ValidationError("模拟支付仅在本地环境开放。")
        order = get_object_or_404(
            ProviderOrder.objects.select_related("payment_order"),
            order_no=order_no,
            customer=request.user,
        )
        if order.paid_at and order.status != ProviderOrder.Status.PENDING_PAYMENT:
            return Response({"data": ProviderOrderSerializer(order).data})
        if order.status != ProviderOrder.Status.PENDING_PAYMENT:
            raise ValidationError({"status": "订单不在待支付状态。"})
        if order.payment_expires_at <= timezone.now():
            apply_provider_order_payment_success(
                order_no=order_no,
                customer_id=request.user.pk,
                channel=ProviderOrderPaymentOrder.Channel.MOCK_WECHAT,
                gateway_trade_no=f"EXPIRED-{order_no}",
                paid_amount=order.payable_amount,
                signature_verified=True,
            )
            return Response({"error": {"status": "支付已超时，档期已释放。"}}, status=409)
        payment, _ = create_provider_order_payment_order(order)
        channel = ProviderOrderPaymentOrder.Channel.MOCK_WECHAT
        result = get_provider_order_payment_gateway(channel).confirm_payment(
            payment_no=payment.payment_no,
            amount=payment.payable_amount,
        )
        order, _payment, _changed = apply_provider_order_payment_success(
            order_no=order_no,
            customer_id=request.user.pk,
            channel=channel,
            gateway_trade_no=result.gateway_trade_no,
            paid_amount=result.paid_amount,
            signature_verified=result.signature_verified,
        )
        return Response({"data": ProviderOrderSerializer(order).data})


class ProviderOrderPaymentSessionView(ProviderOrderDetailView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "provider_order_payment_create"

    def post(self, request, order_no):
        unexpected_fields = set(request.data.keys()) - {"payment_scene"}
        if unexpected_fields:
            raise ValidationError(
                {"non_field_errors": "支付金额和商品信息由服务端订单生成，无需提交请求体。"}
            )
        input_serializer = ProviderOrderPaymentSessionInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        payment_scene = input_serializer.validated_data["payment_scene"]
        order = self.get_object(request, order_no)
        sub_openid = ""
        if payment_scene == "official_account":
            app_id = settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip()
            if not app_id:
                raise WechatOAuthConfigurationError()
            sub_openid = get_official_account_openid(
                user_id=request.user.pk,
                app_id=app_id,
            )
            if not sub_openid:
                raise ValidationError(
                    {"authorization": "请先在微信服务号内完成网页授权。"}
                )
        result, created = create_provider_order_huifu_payment_session(
            order_id=order.pk,
            customer_id=request.user.pk,
            payment_scene=payment_scene,
            sub_openid=sub_openid,
        )
        return Response(
            {
                "data": {
                    "invoke_type": (
                        "WECHAT_JSAPI"
                        if result.trade_type == "T_JSAPI"
                        else "WECHAT_APP"
                    ),
                    "pay_info": result.pay_info,
                }
            },
            status=201 if created else 200,
        )


class ProviderOrderPaymentStatusView(ProviderOrderDetailView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "provider_order_payment_status"

    def post(self, request, order_no):
        order = self.get_object(request, order_no)
        confirm_provider_order_huifu_payment_status(
            order_no=order.order_no,
            customer_id=request.user.pk,
        )
        order = self.get_object(request, order_no)
        return Response({"data": ProviderOrderSerializer(order).data})


class ProviderOrderPaymentAuthorizationView(ProviderOrderDetailView):
    def get(self, request, order_no):
        order = self.get_object(request, order_no)
        if order.status != ProviderOrder.Status.PENDING_PAYMENT:
            raise ValidationError({"order": "当前订单状态不允许发起支付。"})
        if order.payment_expires_at <= timezone.now():
            raise ValidationError({"order": "订单支付时限已过，请重新下单。"})
        authorization = build_payment_authorization(
            user_id=request.user.pk,
            order_no=order.order_no,
        )
        return Response({"data": authorization})


class WechatOfficialOAuthCallbackView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return_url = complete_payment_authorization(
            code=request.query_params.get("code", ""),
            state=request.query_params.get("state", ""),
        )
        return redirect(return_url)


class HuifuPaymentNotificationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        acknowledgement = process_huifu_payment_notification(
            resp_data=request.data.get("resp_data", ""),
            sign=request.data.get("sign", ""),
        )
        return HttpResponse(acknowledgement, content_type="text/plain; charset=utf-8")


class CurrentProviderOrderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider = current_approved_provider(request)
        query = ProviderOrderManageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        orders = ProviderOrder.objects.filter(
            provider=provider,
            paid_at__isnull=False,
        ).select_related(
            "customer", "provider__user", "service__category", "arrival_photo",
            "payment_order", "settlement",
        ).prefetch_related("refund_orders", "after_sales_cases")
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
                "customer", "provider__user", "service__category", "arrival_photo",
                "payment_order", "settlement",
            ).prefetch_related("refund_orders", "after_sales_cases"),
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
            create_order_notification(
                order=order,
                event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
                title="订单已转客服处理",
                content="达人未在时限内接单，平台客服将继续协助处理。",
            )
            return Response(
                {"error": {"status": f"订单已超过{timeout}分钟接单时限，请联系客服。"}},
                status=409,
            )
        order.status = ProviderOrder.Status.PENDING_SERVICE
        order.accepted_at = timezone.now()
        order.save(update_fields=("status", "accepted_at", "updated_at"))
        cancel_provider_acceptance_timeout(order_no, "provider_accepted")
        create_order_notification(
            order=order,
            event_type=UserNotification.EventType.ORDER_ACCEPTED,
            title="达人已接单",
            content=f"{order.provider_name_snapshot}已确认接单，请按预约时间前往集合地点。",
        )
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
            create_order_notification(
                order=order,
                event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
                title="订单已转客服处理",
                content="达人未在时限内接单，平台客服将继续协助处理。",
            )
            return Response(
                {"error": {"status": f"订单已超过{timeout}分钟接单时限，请联系客服。"}},
                status=409,
            )
        rejected_at = timezone.now()
        order.status = ProviderOrder.Status.PENDING_SUPPORT
        order.provider_rejected_at = rejected_at
        order.provider_rejection_reason = ""
        order.support_contact_deadline_at = rejected_at + PROVIDER_REJECTION_SUPPORT_TIMEOUT
        order.support_contacted_at = None
        order.support_contacted_by = None
        order.save(
            update_fields=(
                "status",
                "provider_rejected_at",
                "provider_rejection_reason",
                "support_contact_deadline_at",
                "support_contacted_at",
                "support_contacted_by",
                "updated_at",
            )
        )
        cancel_provider_acceptance_timeout(order_no, "provider_rejected")
        register_provider_rejection_support_timeout(order)
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=None,
            action="provider_order.provider_reject",
            target_type="provider_order",
            target_id=order.order_no,
            before={"status": ProviderOrder.Status.PENDING_ACCEPTANCE},
            after={
                "status": order.status,
                "provider_id": order.provider_id,
                "provider_rejected_at": rejected_at.isoformat(),
                "support_contact_deadline_at": order.support_contact_deadline_at.isoformat(),
            },
            request_id=request.headers.get("X-Request-ID", ""),
            ip_address=client_ip(request),
        )
        create_order_notification(
            order=order,
            event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
            title="订单已转客服处理",
            content="达人暂时无法接单，平台客服将在15分钟内联系你；未完成有效联系时系统将自动全额退款。",
        )
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
        create_order_notification(
            order=order,
            event_type=UserNotification.EventType.ORDER_DEPARTED,
            title="达人已出发",
            content=f"{order.provider_name_snapshot}已前往集合地点，请留意联系。",
        )
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
        create_order_notification(
            order=order,
            event_type=UserNotification.EventType.ORDER_STARTED,
            title="服务已经开始",
            content="本次达人服务已开始，平台正在记录履约状态。",
        )
        return Response({"data": ProviderOrderManageSerializer(order).data})


class CurrentProviderOrderCompleteView(CurrentProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = self.get_object(request, order_no, for_update=True)
        if (
            order.status == ProviderOrder.Status.PENDING_CONFIRMATION
            and order.completion_submitted_at
        ):
            if not order.confirmation_expires_at:
                order.confirmation_expires_at = order.completion_submitted_at + timedelta(
                    days=platform_operation_rules()[
                        "provider_order_confirmation_timeout_days"
                    ]
                )
                order.save(update_fields=("confirmation_expires_at", "updated_at"))
            register_provider_order_confirmation_timeout(order)
            return Response({"data": ProviderOrderManageSerializer(order).data})
        if order.status != ProviderOrder.Status.IN_SERVICE:
            raise ValidationError({"status": "订单不在服务中状态。"})
        completed_at = timezone.now()
        order.status = ProviderOrder.Status.PENDING_CONFIRMATION
        order.completion_submitted_at = completed_at
        order.confirmation_expires_at = completed_at + timedelta(
            days=platform_operation_rules()["provider_order_confirmation_timeout_days"]
        )
        order.save(
            update_fields=(
                "status", "completion_submitted_at", "confirmation_expires_at", "updated_at",
            )
        )
        register_provider_order_confirmation_timeout(order)
        create_order_notification(
            order=order,
            event_type=UserNotification.EventType.ORDER_COMPLETION_SUBMITTED,
            title="达人已提交服务完成",
            content="请确认本次服务是否完成；逾期未操作，系统将按规则自动确认。",
        )
        return Response({"data": ProviderOrderManageSerializer(order).data})


class ProviderOrderConfirmCompletionView(ProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = get_object_or_404(
            ProviderOrder.objects.select_related(
                "provider__user", "service__category", "arrival_photo", "payment_order",
                "settlement",
            ).select_for_update(of=("self",)),
            order_no=order_no,
            customer=request.user,
        )
        if order.status == ProviderOrder.Status.PENDING_REVIEW and (
            order.customer_confirmed_at or order.auto_confirmed_at
        ):
            return Response({"data": ProviderOrderSerializer(order).data})
        if order.status != ProviderOrder.Status.PENDING_CONFIRMATION:
            raise ValidationError({"status": "订单不在待确认状态。"})
        order.status = ProviderOrder.Status.PENDING_REVIEW
        order.customer_confirmed_at = timezone.now()
        order.save(update_fields=("status", "customer_confirmed_at", "updated_at"))
        cancel_provider_order_confirmation_timeout(
            order.order_no, "customer_confirmed_completion"
        )
        ensure_provider_order_settlement(order_no=order.order_no)
        order = ProviderOrder.objects.select_related(
            "provider__user", "service__category", "arrival_photo", "payment_order",
            "settlement",
        ).prefetch_related("review__images", "refund_orders", "after_sales_cases").get(pk=order.pk)
        return Response({"data": ProviderOrderSerializer(order).data})


class ProviderOrderReviewView(ProviderOrderDetailView):
    @transaction.atomic
    def post(self, request, order_no):
        order = get_object_or_404(
            ProviderOrder.objects.select_for_update().select_related("provider"),
            order_no=order_no,
            customer=request.user,
        )
        if order.status == ProviderOrder.Status.COMPLETED and hasattr(order, "review"):
            return Response({"data": ProviderOrderSerializer(order).data})
        if order.status != ProviderOrder.Status.PENDING_REVIEW:
            raise ValidationError({"status": "订单当前不在待评价状态。"})
        serializer = ProviderOrderReviewInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data.copy()
        image_ids = data.pop("image_ids", [])
        unique_image_ids = set(image_ids)
        images = list(
            MediaAsset.objects.filter(
                id__in=unique_image_ids,
                owner=request.user,
                category=MediaAsset.Category.REVIEW_IMAGE,
                status=MediaAsset.Status.UPLOADED,
                provider_order_reviews__isnull=True,
            )
        )
        if len(images) != len(unique_image_ids):
            raise ValidationError({"image_ids": "评价图片不存在或不属于当前用户。"})
        review = ProviderOrderReview.objects.create(
            order=order,
            customer=request.user,
            provider=order.provider,
            **data,
        )
        review.images.set(images)
        order.status = ProviderOrder.Status.COMPLETED
        order.save(update_fields=("status", "updated_at"))
        refresh_provider_review_metrics(order.provider)
        return Response({"data": ProviderOrderSerializer(order).data})


class CurrentUserProviderReviewListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = ProviderOrderReviewListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = query.validated_data["page"]
        page_size = query.validated_data["page_size"]
        reviews = ProviderOrderReview.objects.filter(customer=request.user).select_related(
            "order", "provider__user", "customer"
        ).prefetch_related("images")
        total = reviews.count()
        items = reviews[(page - 1) * page_size : page * page_size]
        return Response(
            {"data": {
                "items": MyProviderOrderReviewSerializer(items, many=True).data,
                "pagination": {"page": page, "page_size": page_size, "total": total},
            }}
        )
