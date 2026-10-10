from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from growth.models import ProviderInviteConfig, ProviderInviteReward, ProviderInviteSource
from growth.provider_invites import config_payload, reward_payload, source_payload, qr_data, require_invite_h5_base
from .access import client_ip, resolve_admin_access
from .models import AdminAuditLog


def access_for(request, permission="provider_invite.view"):
    access = resolve_admin_access(request.user)
    access.require("provider_invite.view")
    access.require(permission)
    if not access.all_data:
        raise PermissionDenied("达人邀请与现金奖励仅限全平台授权人员管理。")
    return access


def audit(request, access, action, target, before, after):
    AdminAuditLog.objects.create(
        actor=request.user,
        organization=access.member.organization if access.member else None,
        action=f"provider_invite.{action}",
        target_type="provider_invite",
        target_id=str(target),
        before=before,
        after=after,
        ip_address=client_ip(request),
    )


class ConfigInput(serializers.Serializer):
    enabled = serializers.BooleanField()
    store_reward_amount = serializers.IntegerField(min_value=0, max_value=1000000)
    provider_reward_amount = serializers.IntegerField(min_value=0, max_value=1000000)
    revision = serializers.IntegerField(min_value=0)

    def validate(self, attrs):
        if attrs["enabled"]:
            require_invite_h5_base()
        if (
            attrs["enabled"]
            and min(attrs["store_reward_amount"], attrs["provider_reward_amount"]) <= 0
        ):
            raise ValidationError("启用前请分别设置门店、达人奖励金额，金额必须大于0。")
        return attrs


class SourceInput(serializers.Serializer):
    name = serializers.CharField(min_length=2, max_length=80)
    active = serializers.BooleanField(default=True)
    revision = serializers.IntegerField(min_value=0, required=False)


class PageInput(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)
    status = serializers.ChoiceField(choices=ProviderInviteReward.Status.values, required=False)
    kind = serializers.ChoiceField(choices=ProviderInviteSource.Kind.values, required=False)
    search = serializers.CharField(max_length=80, required=False, allow_blank=True)


class FinanceInput(serializers.Serializer):
    action = serializers.ChoiceField(choices=("approve", "reject", "paid"))
    revision = serializers.IntegerField(min_value=0)
    note = serializers.CharField(min_length=2, max_length=500)
    confirmed = serializers.BooleanField()
    transfer_reference = serializers.CharField(max_length=100, required=False)
    paid_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        if not attrs["confirmed"]:
            raise ValidationError("请确认核验结果后再操作。")
        if attrs["action"] == "paid":
            if not attrs.get("transfer_reference") or not attrs.get("paid_at"):
                raise ValidationError("请填写唯一转账流水号、实际打款时间及收款人核验说明。")
            if attrs["paid_at"] > timezone.now():
                raise ValidationError("实际打款时间不能晚于当前时间。")
        return attrs


class AdminProviderInviteConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access_for(request)
        return Response({"data": config_payload()})

    @transaction.atomic
    def put(self, request):
        access = access_for(request, "provider_invite.manage")
        serializer = ConfigInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        config, _ = ProviderInviteConfig.objects.select_for_update().get_or_create(pk=1)
        values = serializer.validated_data
        if config.revision != values["revision"]:
            raise ValidationError("配置已更新，请刷新后重试。")
        before = config_payload(config)
        for key in ("enabled", "store_reward_amount", "provider_reward_amount"):
            setattr(config, key, values[key])
        config.revision += 1
        config.save()
        after = config_payload(config)
        audit(request, access, "config", "1", before, after)
        return Response({"data": after})


class AdminProviderInviteSourcesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access_for(request)
        serializer = PageInput(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        qs = ProviderInviteSource.objects.order_by("-id")
        if data.get("kind"):
            qs = qs.filter(kind=data["kind"])
        if data.get("search"):
            qs = qs.filter(Q(name__icontains=data["search"]) | Q(code__icontains=data["search"]))
        page, size = data["page"], data["page_size"]
        return Response(
            {
                "data": {
                    "items": [source_payload(s) for s in qs[(page - 1) * size : page * size]],
                    "pagination": {"page": page, "page_size": size, "total": qs.count()},
                }
            }
        )

    @transaction.atomic
    def post(self, request):
        access = access_for(request, "provider_invite.manage")
        serializer = SourceInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = ProviderInviteSource.objects.create(
            kind="store",
            name=serializer.validated_data["name"],
            active=serializer.validated_data["active"],
        )
        audit(request, access, "source.create", source.public_id, {}, source_payload(source))
        return Response({"data": source_payload(source)}, status=201)


class AdminProviderInviteSourceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, source_id):
        access_for(request)
        source = get_object_or_404(ProviderInviteSource, public_id=source_id)
        return Response({"data": {**source_payload(source), "qr_data": qr_data(source)}})

    @transaction.atomic
    def patch(self, request, source_id):
        access = access_for(request, "provider_invite.manage")
        source = get_object_or_404(
            ProviderInviteSource.objects.select_for_update(), public_id=source_id
        )
        serializer = SourceInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("revision") != source.revision:
            raise ValidationError("邀请来源已更新，请刷新后重试。")
        before = source_payload(source)
        source.name, source.active = data["name"], data["active"]
        source.revision += 1
        source.save()
        audit(request, access, "source.update", source.public_id, before, source_payload(source))
        return Response({"data": source_payload(source)})


class AdminProviderInviteRewardsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access_for(request)
        serializer = PageInput(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        qs = ProviderInviteReward.objects.select_related(
            "source", "profile", "invitee", "reviewed_by", "paid_by"
        )
        if data.get("status"):
            qs = qs.filter(status=data["status"])
        if data.get("kind"):
            qs = qs.filter(source__kind=data["kind"])
        if data.get("search"):
            value = data["search"]
            qs = qs.filter(
                Q(source_name_snapshot__icontains=value)
                | Q(code_snapshot__icontains=value)
                | Q(invitee__phone__icontains=value)
                | Q(profile__application_real_name__icontains=value)
            )
        page, size = data["page"], data["page_size"]
        summary = list(
            qs.order_by().values("status").annotate(count=Count("id"), amount=Sum("amount"))
        )
        return Response(
            {
                "data": {
                    "items": [
                        reward_payload(r, admin=True) for r in qs[(page - 1) * size : page * size]
                    ],
                    "summary": summary,
                    "pagination": {"page": page, "page_size": size, "total": qs.count()},
                }
            }
        )


class AdminProviderInviteRewardActionView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, reward_id):
        serializer = FinanceInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        access = access_for(
            request, "provider_invite.pay" if data["action"] == "paid" else "provider_invite.review"
        )
        reward = get_object_or_404(
            ProviderInviteReward.objects.select_for_update(), public_id=reward_id
        )
        if reward.revision != data["revision"]:
            raise ValidationError("奖励记录已更新，请刷新后重试，勿重复打款。")
        before = {"status": reward.status, "revision": reward.revision, "amount": reward.amount}
        if data["action"] in ("approve", "reject"):
            if reward.status != "pending":
                raise ValidationError("仅待审核奖励可以审核。")
            reward.status = "approved" if data["action"] == "approve" else "rejected"
            reward.reviewed_by, reward.reviewed_at = request.user, timezone.now()
            reward.review_note = data["note"]
        else:
            if reward.status != "approved":
                raise ValidationError("仅财务审核通过的奖励可登记打款。")
            if data["paid_at"] < reward.reviewed_at:
                raise ValidationError("请先完成财务审核，再进行人工打款。")
            reward.status = "paid"
            reward.paid_by, reward.paid_at = request.user, data["paid_at"]
            reward.transfer_reference, reward.payout_note = data["transfer_reference"], data["note"]
        reward.revision += 1
        try:
            with transaction.atomic():
                reward.save()
        except IntegrityError as exc:
            raise ValidationError("该转账流水已登记，请核实，勿重复打款。") from exc
        audit(
            request,
            access,
            data["action"],
            reward.public_id,
            before,
            {
                "status": reward.status,
                "revision": reward.revision,
                "amount": reward.amount,
                "note": data["note"],
                "transfer_reference": reward.transfer_reference,
            },
        )
        return Response({"data": reward_payload(reward, admin=True)})
