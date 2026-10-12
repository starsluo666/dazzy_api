"""Read-only, user-scoped referral statistics (not provider recruitment rewards)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import BigIntegerField, Count, F, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from growth.models import Invitation
from orders.models import ProviderOrder, ProviderOrderPaymentOrder, ProviderOrderRefundOrder

from .access import resolve_admin_access
from .serializers import mask_phone
from .views import scoped_users


BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


class UserInvitationQuerySerializer(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=10)

    def validate(self, attrs):
        start, end = attrs.get("date_from"), attrs.get("date_to")
        for field in ("date_from", "date_to"):
            if attrs.get(field) and attrs[field].year < 2:
                raise serializers.ValidationError({field: "日期超出支持范围。"})
        if start and end and start > end:
            raise serializers.ValidationError({"date_to": "结束日期不能早于开始日期。"})
        if end == date.max:
            raise serializers.ValidationError({"date_to": "结束日期超出支持范围。"})
        return attrs


def user_identity(user):
    return {
        "public_id": str(user.public_id),
        "nickname": user.nickname or "未设置昵称",
        "phone_masked": mask_phone(user.phone),
    }


def qualifying_orders(invitees, access, params):
    # paid_at also supports legacy paid orders without a payment-order row.
    orders = (
        ProviderOrder.objects.filter(
            customer_id__in=invitees,
            paid_at__isnull=False,
        )
        .exclude(
            status__in=(
                ProviderOrder.Status.PENDING_PAYMENT,
                ProviderOrder.Status.CANCELLED,
                ProviderOrder.Status.REFUNDED,
            )
        )
        .exclude(
            payment_order__status__in=(
                ProviderOrderPaymentOrder.Status.PENDING_PAYMENT,
                ProviderOrderPaymentOrder.Status.CLOSED,
                ProviderOrderPaymentOrder.Status.REFUNDED,
            )
        )
    )
    if not access.all_data:
        orders = orders.filter(provider__service_city_code__in=access.city_codes)
    if start := params.get("date_from"):
        orders = orders.filter(created_at__gte=datetime.combine(start, time.min, BUSINESS_TIMEZONE))
    if end := params.get("date_to"):
        orders = orders.filter(
            created_at__lt=datetime.combine(end + timedelta(days=1), time.min, BUSINESS_TIMEZONE)
        )

    # Use a subquery so multiple refund rows never multiply the order count.
    refunds = (
        ProviderOrderRefundOrder.objects.filter(
            order_id=OuterRef("pk"),
            status=ProviderOrderRefundOrder.Status.SUCCEEDED,
        )
        .order_by()
        .values("order_id")
        .annotate(amount=Sum("refund_amount"))
        .values("amount")
    )
    return orders.alias(
        successful_refund_amount=Coalesce(Subquery(refunds), 0, output_field=BigIntegerField()),
    ).exclude(
        Q(successful_refund_amount__gt=0) & Q(successful_refund_amount__gte=F("payable_amount"))
    )


class AdminUserInvitationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, public_id):
        access = resolve_admin_access(request.user)
        access.require("user.view")
        visible_users = scoped_users(access)
        user = get_object_or_404(visible_users, public_id=public_id)
        query = UserInvitationQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        received = Invitation.objects.filter(invitee=user).select_related("inviter").first()
        inviter = None
        if received and visible_users.filter(pk=received.inviter_id).exists():
            inviter = {**user_identity(received.inviter), "invited_at": received.registered_at}

        invitations = Invitation.objects.filter(
            inviter=user,
            invitee_id__in=visible_users.order_by().values("pk"),
        )
        total = invitations.count()
        orders = qualifying_orders(invitations.order_by().values("invitee_id"), access, params)
        summary = orders.aggregate(
            order_count=Count("pk"), ordering_user_count=Count("customer_id", distinct=True)
        )
        per_user = (
            orders.filter(customer_id=OuterRef("invitee_id")).order_by().values("customer_id")
        )
        rows = (
            invitations.select_related("invitee")
            .annotate(
                order_count=Coalesce(
                    Subquery(per_user.annotate(value=Count("pk")).values("value")), 0
                ),
                latest_order_at=Subquery(
                    per_user.annotate(value=Max("created_at")).values("value")
                ),
            )
            .order_by(F("latest_order_at").desc(nulls_last=True), "-registered_at", "-id")
        )
        page, size = params["page"], params["page_size"]
        return Response(
            {
                "data": {
                    "inviter": inviter,
                    "inviter_restricted": bool(received and not inviter),
                    "items": [
                        {
                            **user_identity(item.invitee),
                            "invited_at": item.registered_at,
                            "order_count": item.order_count,
                            "latest_order_at": item.latest_order_at,
                        }
                        for item in rows[(page - 1) * size : page * size]
                    ],
                    "summary": {"invitee_count": total, **summary},
                    "pagination": {"page": page, "page_size": size, "total": total},
                    "filters": {
                        "date_from": params.get("date_from"),
                        "date_to": params.get("date_to"),
                        "timezone": "Asia/Shanghai",
                    },
                    "data_scope": "all" if access.all_data else "cities",
                }
            }
        )
