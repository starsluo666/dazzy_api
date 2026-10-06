"""Durable, explicitly confirmed coupon campaigns; never issue from a preview."""
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .coupons import issue_coupon
from .models import CouponIssueBatch, CouponIssueRecipient

logger = logging.getLogger(__name__)


def eligible_recipients():
    return get_user_model().objects.filter(
        is_active=True, account_status="active", is_staff=False, is_superuser=False,
        backoffice_memberships__isnull=True,
    )


def require_batch_permission(access):
    access.require("coupon.issue")
    if not access.all_data:
        raise PermissionDenied("仅有全平台数据权限的发券人员可操作全员发放。")


def template_snapshot(template):
    return {"name": template.name, "face_amount": template.face_amount,
            "min_order_amount": template.min_order_amount, "valid_days": template.valid_days,
            "description": template.description}


def batch_payload(batch):
    counts = dict(batch.recipients.values("status").annotate(n=Count("pk")).values_list("status", "n"))
    total = sum(counts.values())
    return {
        "public_id": str(batch.public_id), "status": batch.status,
        "template": batch.template_snapshot, "total": total,
        "total_face_amount": total * batch.template_snapshot["face_amount"],
        "pending": counts.get("pending", 0), "issued": counts.get("issued", 0),
        "failed": counts.get("failed", 0), "skipped": counts.get("skipped", 0),
        "created_at": batch.created_at, "confirmed_at": batch.confirmed_at,
        "finished_at": batch.finished_at,
        "preview_expires_at": batch.created_at + timedelta(minutes=15),
    }


@transaction.atomic
def process_coupon_batch(batch_id, *, chunk_size=100):
    # All workers serialize on the batch. One transaction commits coupon, notice,
    # recipient state together; interruption rolls the chunk back for the next tick.
    batch = CouponIssueBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status not in ("queued", "running"):
        return
    batch.status = "running"
    batch.save(update_fields=("status",))
    recipients = list(batch.recipients.filter(status="pending").order_by("id")[:chunk_size])
    for recipient in recipients:
        try:
            with transaction.atomic():
                user = get_user_model().objects.select_for_update().get(pk=recipient.user_id)
                if not eligible_recipients().filter(pk=user.pk).exists():
                    recipient.status = "skipped"
                    recipient.error = "账号当前不符合发放条件"
                else:
                    coupon = issue_coupon(
                        owner=user, template=batch.template, template_snapshot=batch.template_snapshot,
                        issued_by=batch.created_by, source="bulk_manual",
                    )
                    recipient.coupon = coupon
                    recipient.status = "issued"
                    recipient.error = ""
                recipient.save(update_fields=("coupon", "status", "error"))
        except Exception:
            logger.exception("Coupon batch recipient failed: batch=%s recipient=%s", batch.pk, recipient.pk)
            # Deliberately do not expose raw database errors / user data in the UI.
            CouponIssueRecipient.objects.filter(pk=recipient.pk).update(
                status="failed", error="发放失败，请重试该批次中的失败项",
            )
    if not batch.recipients.filter(status="pending").exists():
        batch.status = "partial" if batch.recipients.filter(status="failed").exists() else "completed"
        batch.finished_at = timezone.now()
        batch.save(update_fields=("status", "finished_at"))


def confirm_batch(batch, template):
    if batch.status != "preview":
        return False
    if timezone.now() > batch.created_at + timedelta(minutes=15):
        raise ValidationError("预览已过期，请重新预览人数和优惠券规则。")
    if not template.is_active or template_snapshot(template) != batch.template_snapshot:
        raise ValidationError("优惠券模板已变更或停用，请重新预览后确认。")
    if not batch.recipients.exists():
        raise ValidationError("没有符合条件的用户。")
    batch.status = "queued"
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=("status", "confirmed_at"))
    return True
