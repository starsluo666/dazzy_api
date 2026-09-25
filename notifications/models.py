import uuid

from django.conf import settings
from django.db import models


class UserNotification(models.Model):
    class Category(models.TextChoices):
        SUPPORT = "support", "客服"
        ORDER = "order", "订单"
        ACTIVITY = "activity", "活动"
        SYSTEM = "system", "系统"

    class EventType(models.TextChoices):
        COUPON_ISSUED = "coupon_issued", "优惠券已发放"
        COUPON_REVOKED = "coupon_revoked", "优惠券已撤销"
        SUPPORT_REPLY = "support_reply", "客服回复"
        SUPPORT_RESULT = "support_result", "工单处理结果"
        SUPPORT_REVIEW_RESULT = "support_review_result", "工单复核结果"

        ORDER_PAYMENT_SUCCESS = "order_payment_success", "订单支付成功"
        ORDER_ACCEPTED = "order_accepted", "达人已接单"
        ORDER_PENDING_SUPPORT = "order_pending_support", "订单转客服处理"
        ORDER_DEPARTED = "order_departed", "达人已出发"
        ORDER_STARTED = "order_started", "服务已开始"
        ORDER_COMPLETION_SUBMITTED = (
            "order_completion_submitted",
            "达人提交完成",
        )
        ORDER_AUTO_CONFIRMED = "order_auto_confirmed", "订单自动确认"
        ORDER_AFTER_SALES_STARTED = "order_after_sales_started", "订单售后处理中"
        ORDER_AFTER_SALES_RESULT = "order_after_sales_result", "订单售后结果"
        ORDER_REFUND_COMPLETED = "order_refund_completed", "订单退款完成"
        ORDER_REVIEW_RESULT = "order_review_result", "评价审核结果"
        PROVIDER_NEW_ORDER = "provider_new_order", "达人收到新订单"
        PROVIDER_ORDER_SETTLED = "provider_order_settled", "达人订单结算完成"

        ACTIVITY_PUBLISH_SUBMITTED = (
            "activity_publish_submitted",
            "活动提交审核",
        )
        ACTIVITY_REVIEW_RESULT = "activity_review_result", "活动审核结果"
        ACTIVITY_SIGNUP_SUCCESS = "activity_signup_success", "活动报名成功"
        ACTIVITY_FORMED = "activity_formed", "活动已成局"
        ACTIVITY_REFUND_COMPLETED = "activity_refund_completed", "活动退款完成"
        ACTIVITY_CANCELLED = "activity_cancelled", "活动已取消"
        ACTIVITY_FAILED_TO_FORM = "activity_failed_to_form", "活动未成局"
        ACTIVITY_STARTED = "activity_started", "活动已开始"
        ACTIVITY_COMPLETED = "activity_completed", "活动已结束"
        ACTIVITY_AFTER_SALES_RESULT = (
            "activity_after_sales_result",
            "活动售后结果",
        )
        ACTIVITY_SETTLED = "activity_settled", "活动结算完成"

        PROVIDER_APPLICATION_RESULT = (
            "provider_application_result",
            "达人申请审核结果",
        )
        PROVIDER_STATUS_CHANGED = "provider_status_changed", "达人资格变更"
        PROVIDER_CREDIT_CHANGED = "provider_credit_changed", "达人信用分变更"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="接收用户",
    )
    category = models.CharField("通知分类", max_length=20, choices=Category)
    event_type = models.CharField("事件类型", max_length=40, choices=EventType)
    title = models.CharField("标题", max_length=100)
    content = models.CharField("通知内容", max_length=500)
    target_type = models.CharField("关联对象类型", max_length=32, blank=True)
    target_id = models.CharField("关联对象标识", max_length=64, blank=True)
    target_title = models.CharField("关联对象标题", max_length=160, blank=True)
    action_text = models.CharField("操作文案", max_length=32, blank=True)
    action_url = models.CharField("客户端跳转地址", max_length=255, blank=True)
    dedupe_key = models.CharField("幂等键", max_length=160, unique=True)
    read_at = models.DateTimeField("已读时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_notification"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("recipient", "read_at", "-created_at"),
                name="notif_rec_read_created_idx",
            ),
            models.Index(
                fields=("recipient", "category", "-created_at"),
                name="notif_rec_cat_created_idx",
            ),
        ]
        verbose_name = "站内通知"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.recipient_id} / {self.title}"
