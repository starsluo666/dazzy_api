from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from backoffice.access import client_ip, resolve_admin_access
from backoffice.models import AdminAuditLog

from .admin_serializers import AdminGrowthConfigSerializer, AdminInvitationQuerySerializer
from .models import Invitation
from .services import campaign_payload, get_campaign_config


def _access(request, permission):
    access = resolve_admin_access(request.user)
    access.require(permission)
    if not access.all_data:
        raise PermissionDenied("拉新规则与邀请记录为平台级数据，仅限平台授权账号操作。")
    return access


def mask_phone(phone):
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone


def invitation_payload(item):
    return {
        "public_id": str(item.public_id),
        "inviter_public_id": str(item.inviter.public_id),
        "inviter_name": item.inviter.nickname or "未设置昵称",
        "inviter_phone_masked": mask_phone(item.inviter.phone),
        "invitee_public_id": str(item.invitee.public_id),
        "invitee_name": item.invitee.nickname or "未设置昵称",
        "invitee_phone_masked": mask_phone(item.invitee.phone),
        "status": (
            "first_order_rewarded" if item.first_order_reward_coupon_id else "registered"
        ),
        "registration_rewarded": bool(item.registration_reward_coupon_id),
        "first_order_rewarded": bool(item.first_order_reward_coupon_id),
        "registered_at": item.registered_at,
        "first_order_completed_at": item.first_order_completed_at,
        "order_no": item.first_completed_order.order_no if item.first_completed_order_id else "",
    }


class AdminGrowthConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _access(request, "growth.view")
        return Response({"data": campaign_payload(get_campaign_config())})

    @transaction.atomic
    def patch(self, request):
        access = _access(request, "growth.manage")
        config = get_campaign_config(lock=True)
        before = campaign_payload(config)
        serializer = AdminGrowthConfigSerializer(
            data=request.data,
            partial=True,
            context={"instance": config},
        )
        serializer.is_valid(raise_exception=True)
        config = serializer.save(updated_by=request.user)
        config = get_campaign_config()
        after = campaign_payload(config)
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=access.member.organization if access.member else None,
            action="growth.config.update",
            target_type="growth_campaign_config",
            target_id=str(config.pk),
            before=before,
            after=after,
            ip_address=client_ip(request),
        )
        return Response({"data": after})


class AdminInvitationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _access(request, "growth.view")
        serializer = AdminInvitationQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data
        queryset = Invitation.objects.select_related(
            "inviter",
            "invitee",
            "registration_reward_coupon",
            "first_order_reward_coupon",
            "first_completed_order",
        )
        search = query.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(inviter__nickname__icontains=search)
                | Q(inviter__phone__icontains=search)
                | Q(invitee__nickname__icontains=search)
                | Q(invitee__phone__icontains=search)
            )
        if query.get("status") == "registered":
            queryset = queryset.filter(first_order_reward_coupon__isnull=True)
        elif query.get("status") == "first_order_rewarded":
            queryset = queryset.filter(first_order_reward_coupon__isnull=False)
        total = queryset.count()
        page = query["page"]
        page_size = query["page_size"]
        rows = queryset[(page - 1) * page_size : page * page_size]
        return Response(
            {
                "data": {
                    "items": [invitation_payload(item) for item in rows],
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "generated_at": timezone.now(),
                }
            }
        )
