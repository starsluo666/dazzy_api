from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from mediafiles.models import MediaAsset
from orders.coupon_batches import template_snapshot
from orders.coupon_campaigns import campaign_payload
from orders.coupons import coupon_payload
from orders.models import CouponCampaign, CouponTemplate
from .access import client_ip, resolve_admin_access
from .models import AdminAuditLog


def campaign_access(request, *, write=False):
    access = resolve_admin_access(request.user)
    access.require("coupon_campaign.view")
    if write:
        access.require("coupon_campaign.manage")
    if not access.all_data:
        raise PermissionDenied("领券活动面向全平台，仅限具有全平台权限的运营人员操作。")
    return access


class CampaignInput(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    banner_id = serializers.UUIDField()
    template_public_id = serializers.UUIDField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    stock = serializers.IntegerField(min_value=1, max_value=1000000)
    sort_order = serializers.IntegerField(min_value=0, max_value=9999, default=0)
    revision = serializers.IntegerField(min_value=1, required=False)


class ActionInput(serializers.Serializer):
    action = serializers.ChoiceField(choices=("publish", "offline"))
    revision = serializers.IntegerField(min_value=1)
    confirmed = serializers.BooleanField()


class PageInput(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)


def audit(request, access, campaign, action, before=None):
    # JSON audit stores only stable primitive fields, never datetime objects or PII.
    AdminAuditLog.objects.create(
        actor=request.user, organization=access.member.organization if access.member else None,
        action=f"coupon.campaign.{action}", target_type="coupon_campaign", target_id=str(campaign.public_id),
        before=before or {}, after=audit_snapshot(campaign), ip_address=client_ip(request),
    )


def audit_snapshot(c):
    return {"name": c.name, "banner_id": str(c.banner_id), "template_id": c.template_id,
            "coupon": c.template_snapshot, "stock": c.stock, "status": c.status,
            "sort_order": c.sort_order, "revision": c.revision,
            "starts_at": c.starts_at.isoformat(), "ends_at": c.ends_at.isoformat()}


def valid_banner(asset_id):
    # Lock the asset row shared with batch deletion: deletion cannot pass its
    # reference check while a new campaign is attaching this same asset.
    asset = MediaAsset.objects.select_for_update().filter(
        pk=asset_id, scope="public", status="uploaded", category="operations_image",
        content_type__in=("image/jpeg", "image/png", "image/webp"),
    ).first()
    if not asset:
        raise ValidationError({"banner_id": "请选择素材库中有效的运营图片，不能使用图标或私有资料。"})
    return asset


def apply_input(request, access, data, instance=None):
    if instance and data.get("revision") != instance.revision:
        raise ValidationError("活动已被其他操作更新，请刷新后重试。")
    if data["ends_at"] <= data["starts_at"]:
        raise ValidationError({"ends_at": "结束时间必须晚于开始时间。"})
    if instance and data["stock"] < instance.issued_count:
        raise ValidationError({"stock": "总库存不能低于已领取数量。"})
    # Consistent mutation lock order: campaign -> template -> asset.
    template = get_object_or_404(CouponTemplate.objects.select_for_update(), public_id=data["template_public_id"])
    if instance and instance.published_at and template.pk != instance.template_id:
        raise ValidationError({"template_public_id": "已发布的活动不能更换券种，请新建活动。"})
    if not instance or not instance.published_at:
        if not template.is_active:
            raise ValidationError({"template_public_id": "请选择启用中的优惠券模板。"})
    if not instance or str(instance.banner_id) != str(data["banner_id"]):
        access.require("asset.view")
    banner = valid_banner(data["banner_id"])
    values = {k: data[k] for k in ("name", "starts_at", "ends_at", "stock", "sort_order")}
    values.update(template=template, banner=banner, updated_by=request.user)
    if instance:
        for key, value in values.items():
            setattr(instance, key, value)
        instance.revision += 1
        instance.save()
    else:
        instance = CouponCampaign.objects.create(**values, created_by=request.user)
    return instance


class AdminCouponCampaignListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        campaign_access(request)
        query = PageInput(data=request.query_params)
        query.is_valid(raise_exception=True)
        page, size = query.validated_data["page"], query.validated_data["page_size"]
        qs = CouponCampaign.objects.select_related("banner", "template")
        return Response({"data": {"items": [campaign_payload(c, admin=True) for c in qs[(page - 1) * size:page * size]],
                                  "templates": [{"public_id": str(t.public_id), **template_snapshot(t)}
                                                for t in CouponTemplate.objects.filter(is_active=True)],
                                  "pagination": {"page": page, "page_size": size, "total": qs.count()}}})

    @transaction.atomic
    def post(self, request):
        access = campaign_access(request, write=True)
        serializer = CampaignInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        campaign = apply_input(request, access, serializer.validated_data)
        audit(request, access, campaign, "create")
        return Response({"data": campaign_payload(campaign, admin=True)}, status=201)


class AdminCouponCampaignDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, campaign_id):
        access = campaign_access(request, write=True)
        campaign = get_object_or_404(CouponCampaign.objects.select_for_update(), public_id=campaign_id)
        before = audit_snapshot(campaign)
        serializer = CampaignInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        campaign = apply_input(request, access, serializer.validated_data, campaign)
        audit(request, access, campaign, "update", before)
        return Response({"data": campaign_payload(campaign, admin=True)})

    @transaction.atomic
    def post(self, request, campaign_id):
        access = campaign_access(request, write=True)
        serializer = ActionInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not data["confirmed"]:
            raise ValidationError("请确认本次上架或下架操作。")
        campaign = get_object_or_404(CouponCampaign.objects.select_for_update(), public_id=campaign_id)
        if campaign.revision != data["revision"]:
            raise ValidationError("活动已被其他操作更新，请刷新后重试。")
        before = audit_snapshot(campaign)
        if data["action"] == "publish":
            template = CouponTemplate.objects.select_for_update().get(pk=campaign.template_id)
            valid_banner(campaign.banner_id)
            if campaign.ends_at <= timezone.now():
                raise ValidationError("活动已结束，请先调整领取时间。")
            if campaign.stock <= campaign.issued_count:
                raise ValidationError("没有可领取库存，请先补充库存。")
            if campaign.published_at is None:
                if not template.is_active:
                    raise ValidationError("优惠券模板已停用，不能首次发布。")
                campaign.template_snapshot = template_snapshot(template)
                campaign.published_at = timezone.now()
            campaign.status = "published"
        else:
            campaign.status = "offline"
        campaign.revision += 1
        campaign.updated_by = request.user
        campaign.save()
        audit(request, access, campaign, data["action"], before)
        return Response({"data": campaign_payload(campaign, admin=True)})


class AdminCouponCampaignClaimsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, campaign_id):
        campaign_access(request)
        campaign = get_object_or_404(CouponCampaign, public_id=campaign_id)
        query = PageInput(data=request.query_params)
        query.is_valid(raise_exception=True)
        page, size = query.validated_data["page"], query.validated_data["page_size"]
        claims = campaign.claims.select_related("user", "coupon__template").order_by("-id")
        return Response({"data": {
            "items": [{"user_public_id": str(c.user.public_id), "nickname": c.user.nickname,
                       "phone_masked": f"{c.user.phone[:3]}****{c.user.phone[-4:]}",
                       "claimed_at": c.created_at, "coupon": coupon_payload(c.coupon)}
                      for c in claims[(page - 1) * size:page * size]],
            "pagination": {"page": page, "page_size": size, "total": claims.count()},
        }})
