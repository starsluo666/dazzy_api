"""Scoped staff refund intake and safe local previews. No channel requests."""

from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import ValidationError, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from activities.models import (
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivitySettlement,
)
from activities.services import create_activity_after_sales_case
from orders.models import ProviderOrder, ProviderOrderDistribution, ProviderOrderSettlement
from orders.services import _discounted_order_components
from .access import client_ip, resolve_admin_access
from .models import AdminAuditLog
from .refund_authorization import has, refund_policy
from .serializers import AdminActivityAfterSalesSerializer


def refund_source(access, kind, reference):
    if kind == "provider":
        access.require("order.after_sales.view")
        qs = ProviderOrder.objects.select_related("customer", "provider")
        if not access.all_data:
            qs = qs.filter(provider__service_city_code__in=access.city_codes)
        return get_object_or_404(qs, order_no=reference)
    if kind == "activity":
        access.require("activity_finance.view")
        qs = ActivityParticipationPaymentOrder.objects.select_related(
            "participation__activity", "participation__user"
        )
        if not access.all_data:
            qs = qs.filter(participation__activity__city_code__in=access.city_codes)
        return get_object_or_404(qs, order_no=reference)
    raise NotFound("不支持的订单类型。")


def refund_context(source, kind):
    refunds = source.refund_orders.all()
    refunded = (
        refunds.filter(status="succeeded").aggregate(total=Sum("refund_amount"))["total"] or 0
    )
    occupied = (
        refunds.exclude(status="succeeded").aggregate(total=Sum("refund_amount"))["total"] or 0
    )
    blocked = ""
    if kind == "provider":
        components = _discounted_order_components(source)
        fields = (
            ("service", "服务费", "service_fee_refund_amount"),
            ("other", "其他费用", "other_fee_refund_amount"),
            ("transport", "路费", "transport_fee_refund_amount"),
        )
        rows = [
            {
                "key": key,
                "label": label,
                "paid": components[key],
                "remaining": max(
                    0, components[key] - (refunds.aggregate(total=Sum(field))["total"] or 0)
                ),
            }
            for key, label, field in fields
        ]
        case = source.after_sales_cases.filter(
            status__in=("pending", "processing", "approved")
        ).first()
        if not source.paid_at:
            blocked = "订单尚未支付。"
        elif ProviderOrderDistribution.objects.filter(settlement__order=source).exists():
            blocked = "订单已进入渠道分账流程，请转财务核查；本入口不能回退分账。"
        elif ProviderOrderSettlement.objects.filter(order=source, status="settled").exists():
            blocked = "订单已结算，请转财务核查。"
        customer = source.customer.nickname
        notice = "沿用原规则：部分退款依次退服务费、其他费用、路费；全额退款包含剩余路费。登记售后会暂停自动确认与结算，不自动扣达人信用分。"
    else:
        fields = (
            ("principal", "AA 本金", "aa_principal_amount", "principal_refund_amount"),
            (
                "service_fee",
                "平台服务费",
                "platform_service_fee_amount",
                "service_fee_refund_amount",
            ),
        )
        rows = [
            {
                "key": key,
                "label": label,
                "paid": getattr(source, field),
                "remaining": max(
                    0,
                    getattr(source, field)
                    - (refunds.aggregate(total=Sum(refund_field))["total"] or 0),
                ),
            }
            for key, label, field, refund_field in fields
        ]
        case = source.participation.after_sales_cases.filter(
            Q(status__in=("pending", "processing"))
            | (
                Q(status="approved")
                & (Q(refund_order__isnull=True) | ~Q(refund_order__status="succeeded"))
            )
        ).first()
        if source.status not in ("paid", "partially_refunded"):
            blocked = "报名支付单不在可退款状态。"
        elif ActivitySettlement.objects.filter(
            activity=source.participation.activity, status="settled"
        ).exists():
            blocked = "活动资金已结算，请转财务核查。"
        customer = source.participation.user.nickname
        notice = "仅处理当前报名订单，不取消整个活动。沿用原规则：售后退款获批后取消该用户报名（部分退款也会取消）；登记售后会冻结相关活动结算。"
    remaining = max(0, source.payable_amount - refunded - occupied)
    if not blocked and case:
        blocked = "已有未结束的售后申请，请处理原售后单。"
    if not blocked and occupied:
        blocked = "存在处理中或待核实的退款，请核查原退款单，不能重复申请。"
    if not blocked and not remaining:
        blocked = "本订单已无可退金额。"
    return {
        "reference": source.order_no,
        "customer": customer,
        "paid": source.payable_amount,
        "refunded": refunded,
        "occupied": occupied,
        "remaining": remaining,
        "components": rows,
        "open_case_no": case.case_no if case else "",
        "blocked_reason": blocked,
        "notice": notice,
    }


class StaffRefundContextView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, kind, reference):
        access = resolve_admin_access(request.user)
        data = refund_context(refund_source(access, kind, reference), kind)
        permission = (
            "order.after_sales.create" if kind == "provider" else "activity_after_sales.create"
        )
        data["can_create"] = has(access, permission) and not data["blocked_reason"]
        data["policy"] = refund_policy(request.user, access)
        return Response({"data": data})


class StaffRefundPolicyView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        return Response({"data": refund_policy(request.user, access)})


class ActivityRefundInput(serializers.Serializer):
    requested_principal_amount = serializers.IntegerField(min_value=0)
    requested_service_fee_amount = serializers.IntegerField(min_value=0)
    reason = serializers.CharField(min_length=5, max_length=1000)


class StaffActivityRefundCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, reference):
        access = resolve_admin_access(request.user)
        access.require("activity_after_sales.create")
        source = refund_source(access, "activity", reference)
        participation = ActivityParticipation.objects.select_for_update().get(
            pk=source.participation_id
        )
        source = (
            ActivityParticipationPaymentOrder.objects.select_for_update()
            .select_related("participation__activity", "participation__user")
            .get(pk=source.pk)
        )
        context = refund_context(source, "activity")
        if context["blocked_reason"]:
            raise ValidationError(context["blocked_reason"])
        serializer = ActivityRefundInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        principal, fee = (
            params["requested_principal_amount"],
            params["requested_service_fee_amount"],
        )
        if principal + fee <= 0:
            raise ValidationError("退款申请金额必须大于 0。")
        if (
            principal > context["components"][0]["remaining"]
            or fee > context["components"][1]["remaining"]
        ):
            raise ValidationError("申请金额超过当前可退金额。")
        latest = participation.payment_orders.filter(
            status__in=("paid", "partially_refunded")
        ).first()
        if not latest or latest.pk != source.pk:
            raise ValidationError("请选择当前有效的报名支付单。")
        case, created = create_activity_after_sales_case(
            activity_id=participation.activity_id,
            applicant=participation.user,
            reason="other",
            description=params["reason"],
        )
        if created:
            case.requested_principal_amount, case.requested_service_fee_amount = principal, fee
            case.requested_amount, case.created_by_operator = principal + fee, request.user
            case.save(
                update_fields=(
                    "requested_principal_amount",
                    "requested_service_fee_amount",
                    "requested_amount",
                    "created_by_operator",
                    "updated_at",
                )
            )
            AdminAuditLog.objects.create(
                actor=request.user,
                organization=access.member.organization if access.member else None,
                action="activity.after_sales.create",
                target_type="activity_after_sales_case",
                target_id=case.case_no,
                before={},
                after={
                    "order_no": reference,
                    "requested_amount": case.requested_amount,
                    "reason": params["reason"],
                },
                request_id=request.headers.get("X-Request-ID", ""),
                ip_address=client_ip(request),
            )
        return Response(
            {"data": AdminActivityAfterSalesSerializer(case).data}, status=201 if created else 200
        )
