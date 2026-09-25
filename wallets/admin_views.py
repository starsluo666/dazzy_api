from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backoffice.access import client_ip, resolve_admin_access
from backoffice.models import AdminAuditLog

from .models import RechargeCampaign, RechargeDiscountTier, UserWallet, WalletRechargeOrder
from .services import recharge_order_payload
from .serializers import RechargeCampaignInputSerializer, WalletPaginationSerializer


def _pagination(request):
    serializer = WalletPaginationSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data["page"], serializer.validated_data["page_size"]


def _access(request, permission):
    access = resolve_admin_access(request.user)
    access.require(permission)
    if not access.all_data:
        raise PermissionDenied("钱包与充值为平台级财务数据，仅限平台授权账号操作。")
    return access


def campaign_payload(campaign):
    return {
        "is_enabled": campaign.is_enabled,
        "unit_face_amount": campaign.unit_face_amount,
        "max_quantity_per_order": campaign.max_quantity_per_order,
        "rules_text": campaign.rules_text,
        "tiers": [
            {
                "min_quantity": tier.min_quantity,
                "discount_rate_bps": tier.discount_rate_bps,
            }
            for tier in campaign.discount_tiers.all()
        ],
        "updated_at": campaign.updated_at.isoformat(),
    }


class AdminRechargeCampaignView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _access(request, "wallet.view")
        return Response({"data": campaign_payload(RechargeCampaign.current())})

    @transaction.atomic
    def patch(self, request):
        access = _access(request, "wallet.manage")
        campaign = RechargeCampaign.objects.select_for_update().filter(singleton_key=1).first()
        if campaign is None:
            campaign = RechargeCampaign.objects.create(singleton_key=1)
        before = campaign_payload(campaign)
        allowed = {"is_enabled", "unit_face_amount", "max_quantity_per_order", "rules_text", "tiers"}
        unexpected = set(request.data) - allowed
        if unexpected:
            raise ValidationError({"non_field_errors": f"不支持字段：{', '.join(sorted(unexpected))}"})
        serializer = RechargeCampaignInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        unit_face_amount = data.get("unit_face_amount", campaign.unit_face_amount)
        max_quantity = data.get("max_quantity_per_order", campaign.max_quantity_per_order)
        if unit_face_amount != 100000:
            raise ValidationError({"unit_face_amount": "当前业务规则固定每张面值 1000 元。"})
        if not 1 <= max_quantity <= 99:
            raise ValidationError({"max_quantity_per_order": "单次购买张数须在 1 至 99 之间。"})
        campaign.is_enabled = data.get("is_enabled", campaign.is_enabled)
        campaign.unit_face_amount = unit_face_amount
        campaign.max_quantity_per_order = max_quantity
        campaign.rules_text = data.get("rules_text", campaign.rules_text)
        campaign.updated_by = request.user
        campaign.save()
        if "tiers" not in data and campaign.discount_tiers.filter(min_quantity__gt=max_quantity).exists():
            raise ValidationError({"max_quantity_per_order": "请同时调整超过购买上限的折扣档位。"})
        if "tiers" in data:
            tiers = data["tiers"]
            if not isinstance(tiers, list):
                raise ValidationError({"tiers": "折扣档位必须是数组。"})
            normalized = []
            seen = set()
            for index, item in enumerate(tiers):
                if not isinstance(item, dict):
                    raise ValidationError({"tiers": f"第 {index + 1} 个档位格式无效。"})
                quantity = int(item.get("min_quantity", 0))
                rate = int(item.get("discount_rate_bps", 0))
                if not 1 <= quantity <= max_quantity or quantity in seen:
                    raise ValidationError({"tiers": "档位张数必须唯一且不超过单次上限。"})
                if not 1 <= rate <= 10000:
                    raise ValidationError({"tiers": "折扣比例须在 0.01 折至 10 折之间。"})
                seen.add(quantity)
                normalized.append((quantity, rate))
            campaign.discount_tiers.all().delete()
            RechargeDiscountTier.objects.bulk_create(
                [
                    RechargeDiscountTier(
                        campaign=campaign,
                        min_quantity=quantity,
                        discount_rate_bps=rate,
                    )
                    for quantity, rate in sorted(normalized)
                ]
            )
        campaign.refresh_from_db()
        after = campaign_payload(campaign)
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=access.member.organization if access.member else None,
            action="wallet.recharge_config.update", target_type="recharge_campaign",
            target_id=str(campaign.pk), before=before, after=after,
            ip_address=client_ip(request),
        )
        return Response({"data": after})


class AdminWalletListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _access(request, "wallet.view")
        search = request.query_params.get("search", "").strip()
        queryset = UserWallet.objects.select_related("user").order_by("-updated_at")
        if search:
            queryset = queryset.filter(Q(user__phone__icontains=search) | Q(user__nickname__icontains=search))
        page, page_size = _pagination(request)
        total = queryset.count()
        rows = queryset[(page - 1) * page_size : page * page_size]
        return Response({"data": {"items": [
                {
                    "user_public_id": str(item.user.public_id),
                    "nickname": item.user.nickname,
                    "phone": item.user.phone,
                    "available_balance": item.available_balance,
                    "frozen_balance": item.frozen_balance,
                    "total_balance": item.available_balance + item.frozen_balance,
                    "updated_at": item.updated_at,
                }
                for item in rows
            ], "pagination": {"page": page, "page_size": page_size, "total": total}}})


class AdminRechargeOrderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _access(request, "wallet.view")
        queryset = WalletRechargeOrder.objects.select_related("user").order_by("-created_at")
        search = request.query_params.get("search", "").strip()
        status = request.query_params.get("status", "").strip()
        if search:
            queryset = queryset.filter(
                Q(order_no__icontains=search)
                | Q(user__phone__icontains=search)
                | Q(user__nickname__icontains=search)
            )
        if status:
            queryset = queryset.filter(status=status)
        page, page_size = _pagination(request)
        total = queryset.count()
        rows = queryset[(page - 1) * page_size : page * page_size]
        return Response({"data": {"items": [
                {
                    **recharge_order_payload(item),
                    "nickname": item.user.nickname,
                    "phone": item.user.phone,
                }
                for item in rows
            ], "pagination": {"page": page, "page_size": page_size, "total": total}}})
