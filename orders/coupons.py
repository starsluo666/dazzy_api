"""Single-use provider-order coupons; all money values are in cents."""

from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.operation_settings import platform_operation_rules

from .models import UserCoupon


def issue_coupon(*, owner, source="manual", issued_by=None, template=None, now=None, notify=True):
    now = now or timezone.now()
    if template is not None and not template.is_active:
        raise ValidationError({"template_public_id": "该优惠券模板已停用。"})
    rules = platform_operation_rules() if template is None else None
    coupon = UserCoupon.objects.create(
        owner=owner,
        template=template,
        source=source,
        issued_by=issued_by,
        face_amount=template.face_amount if template else rules["report_coupon_amount"],
        min_order_amount=(
            template.min_order_amount if template else rules["report_coupon_min_order_amount"]
        ),
        expires_at=now + timedelta(
            days=template.valid_days if template else rules["report_coupon_valid_days"]
        ),
    )
    if notify:
        from notifications.models import UserNotification
        from notifications.services import create_notification

        create_notification(
            recipient=owner, category=UserNotification.Category.SYSTEM,
            event_type=UserNotification.EventType.COUPON_ISSUED,
            title=f"收到一张{template.name}" if template else "收到一张优惠券",
            content=(f"已收到 ¥{coupon.face_amount / 100:.2f} 优惠券，"
                     f"达人服务订单金额大于 ¥{coupon.min_order_amount / 100:.2f} 可用。"),
            target_type="coupon", target_id=str(coupon.public_id),
            target_title="我的优惠券", action_text="查看优惠券",
            action_url="/pages/coupons/index", dedupe_key=f"coupon:issued:{coupon.public_id}",
        )
    return coupon


def eligible_coupon(*, owner, public_id, order_amount, lock=False, now=None):
    now = now or timezone.now()
    queryset = UserCoupon.objects
    if lock:
        queryset = queryset.select_for_update()
    coupon = queryset.filter(public_id=public_id, owner=owner).first()
    if coupon is None or coupon.status != UserCoupon.Status.AVAILABLE or coupon.expires_at <= now:
        raise ValidationError({"coupon_id": "优惠券不存在、已使用或已过期。"})
    if order_amount <= coupon.min_order_amount:
        raise ValidationError({"coupon_id": "订单金额未超过优惠券使用门槛。"})
    return coupon


def coupon_discount(coupon, order_amount):
    # The payment gateway requires a positive payable amount.
    return min(coupon.face_amount, order_amount - 1)


def reserve_coupon(*, coupon, order):
    if coupon.status != UserCoupon.Status.AVAILABLE or coupon.reserved_order_id:
        raise ValidationError({"coupon_id": "优惠券已被其他订单使用。"})
    coupon.status = UserCoupon.Status.RESERVED
    coupon.reserved_order = order
    coupon.save(update_fields=("status", "reserved_order"))


def consume_coupon(order, *, now=None):
    coupon = UserCoupon.objects.select_for_update().filter(reserved_order=order).first()
    if coupon and coupon.status == UserCoupon.Status.RESERVED:
        coupon.status = UserCoupon.Status.USED
        coupon.used_at = now or timezone.now()
        coupon.save(update_fields=("status", "used_at"))


def release_coupon(order, *, refunded=False, now=None):
    coupon = UserCoupon.objects.select_for_update().filter(reserved_order=order).first()
    if coupon is None:
        return
    if coupon.status == UserCoupon.Status.RESERVED or (
        refunded and coupon.status == UserCoupon.Status.USED
    ):
        coupon.status = UserCoupon.Status.AVAILABLE
        coupon.reserved_order = None
        coupon.used_at = None
        coupon.save(update_fields=("status", "reserved_order", "used_at"))


def coupon_payload(coupon, *, now=None):
    now = now or timezone.now()
    return {
        "public_id": str(coupon.public_id),
        "template_public_id": str(coupon.template.public_id) if coupon.template_id else None,
        "template_name": coupon.template.name if coupon.template_id else "优惠券",
        "face_amount": coupon.face_amount,
        "min_order_amount": coupon.min_order_amount,
        "expires_at": coupon.expires_at,
        "status": "expired" if coupon.expires_at <= now and coupon.status == UserCoupon.Status.AVAILABLE else coupon.status,
        "source": coupon.source,
        "revoked_at": coupon.revoked_at,
        "revoke_reason": coupon.revoke_reason,
        "created_at": coupon.created_at,
    }
