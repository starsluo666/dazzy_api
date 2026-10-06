from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.coupon_batches import (
    batch_payload, confirm_batch, eligible_recipients, require_batch_permission, template_snapshot,
)
from orders.models import CouponIssueBatch, CouponIssueRecipient, CouponTemplate
from .access import client_ip, resolve_admin_access
from .models import AdminAuditLog


class BatchPreviewInput(serializers.Serializer):
    template_public_id = serializers.UUIDField()
    request_id = serializers.UUIDField()


class BatchActionInput(serializers.Serializer):
    action = serializers.ChoiceField(choices=("confirm", "retry"))
    confirmed = serializers.BooleanField()

    def validate_confirmed(self, value):
        if not value:
            raise serializers.ValidationError("请明确确认本次操作。")
        return value


def audit_batch(request, access, batch, action):
    AdminAuditLog.objects.create(
        actor=request.user, organization=access.member.organization if access.member else None,
        action=f"coupon.batch.{action}", target_type="coupon_batch", target_id=str(batch.public_id),
        after={"template": batch.template_snapshot, "recipients": batch.recipients.count()},
        ip_address=client_ip(request),
    )


class CouponBatchListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        require_batch_permission(access)
        batches = CouponIssueBatch.objects.exclude(status="preview").order_by("-id")[:30]
        return Response({"data": {"items": [batch_payload(batch) for batch in batches]}})

    @transaction.atomic
    def post(self, request):
        access = resolve_admin_access(request.user)
        require_batch_permission(access)
        serializer = BatchPreviewInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        # Same actor's retries serialize before creating recipient snapshots.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        existing = CouponIssueBatch.objects.filter(request_id=data["request_id"]).first()
        if existing:
            if existing.created_by_id != request.user.pk or existing.template.public_id != data["template_public_id"]:
                raise ValidationError("请求标识已被使用，请重新创建预览。")
            return Response({"data": batch_payload(existing)})
        template = get_object_or_404(CouponTemplate.objects.select_for_update(), public_id=data["template_public_id"], is_active=True)
        try:
            with transaction.atomic():
                batch = CouponIssueBatch.objects.create(
                    request_id=data["request_id"], template=template, created_by=request.user,
                    template_snapshot=template_snapshot(template),
                )
        except IntegrityError as exc:
            raise ValidationError("请求标识已被使用，请刷新批次记录核对。") from exc
        pending = []
        for user_id in eligible_recipients().order_by("id").values_list("id", flat=True).iterator(chunk_size=500):
            pending.append(CouponIssueRecipient(batch=batch, user_id=user_id))
            if len(pending) == 500:
                CouponIssueRecipient.objects.bulk_create(pending, batch_size=500)
                pending = []
        if pending:
            CouponIssueRecipient.objects.bulk_create(pending, batch_size=500)
        audit_batch(request, access, batch, "preview")
        return Response({"data": batch_payload(batch)}, status=201)


class CouponBatchDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, batch_id):
        require_batch_permission(resolve_admin_access(request.user))
        return Response({"data": batch_payload(get_object_or_404(CouponIssueBatch, public_id=batch_id))})

    @transaction.atomic
    def post(self, request, batch_id):
        access = resolve_admin_access(request.user)
        require_batch_permission(access)
        serializer = BatchActionInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = get_object_or_404(CouponIssueBatch.objects.select_for_update(), public_id=batch_id)
        action = serializer.validated_data["action"]
        changed = False
        if action == "confirm":
            if batch.created_by_id != request.user.pk:
                raise PermissionDenied("请由创建预览的操作人员确认发放。")
            template = CouponTemplate.objects.select_for_update().get(pk=batch.template_id)
            changed = confirm_batch(batch, template)
        elif batch.status == "partial":
            batch.recipients.filter(status="failed").update(status="pending", error="")
            batch.status, batch.finished_at = "queued", None
            batch.save(update_fields=("status", "finished_at"))
            changed = True
        elif batch.status not in ("queued", "running", "completed"):
            raise ValidationError("该批次尚未确认发放，不能重试。")
        if changed:
            audit_batch(request, access, batch, action)
        return Response({"data": batch_payload(batch)})
