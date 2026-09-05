import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class ScheduledTask(models.Model):
    class Type(models.TextChoices):
        PROVIDER_ORDER_PAYMENT_EXPIRY = (
            "provider_order_payment_expiry",
            "达人订单支付超时",
        )
        PROVIDER_ACCEPTANCE_TIMEOUT = (
            "provider_acceptance_timeout",
            "达人接单超时",
        )
        PROVIDER_ORDER_CONFIRMATION_TIMEOUT = (
            "provider_order_confirmation_timeout",
            "达人订单确认超时",
        )
        PROVIDER_ORDER_SETTLEMENT = (
            "provider_order_settlement",
            "达人订单资金结算",
        )
        ACTIVITY_PARTICIPATION_PAYMENT_EXPIRY = (
            "activity_participation_payment_expiry",
            "活动报名支付超时",
        )
        ACTIVITY_FORMATION_DEADLINE = (
            "activity_formation_deadline",
            "活动成局截止",
        )
        ACTIVITY_START = "activity_start", "活动开始"
        ACTIVITY_COMPLETION = "activity_completion", "活动结束"
        ACTIVITY_SETTLEMENT = "activity_settlement", "活动资金结算"

    class Status(models.TextChoices):
        PENDING = "pending", "待执行"
        RUNNING = "running", "执行中"
        SUCCEEDED = "succeeded", "执行成功"
        FAILED = "failed", "执行失败"
        CANCELLED = "cancelled", "已取消"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    task_type = models.CharField("任务类型", max_length=64, choices=Type)
    business_type = models.CharField("业务对象类型", max_length=40)
    business_key = models.CharField("业务对象标识", max_length=100)
    dedupe_key = models.CharField("幂等键", max_length=220, unique=True)
    payload = models.JSONField("任务参数", default=dict, blank=True)
    status = models.CharField(
        "任务状态", max_length=16, choices=Status, default=Status.PENDING
    )
    scheduled_at = models.DateTimeField("计划执行时间")
    available_at = models.DateTimeField("下次可执行时间")
    attempt_count = models.PositiveSmallIntegerField("已执行次数", default=0)
    max_attempts = models.PositiveSmallIntegerField(
        "最大执行次数",
        default=3,
        validators=(MinValueValidator(1), MaxValueValidator(10)),
    )
    started_at = models.DateTimeField("最近开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("完成时间", null=True, blank=True)
    last_error = models.TextField("最近错误", blank=True)
    result = models.JSONField("执行结果", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scheduled_task"
        ordering = ("-scheduled_at", "-id")
        indexes = [
            models.Index(
                fields=("status", "available_at"),
                name="task_status_available_idx",
            ),
            models.Index(
                fields=("task_type", "status", "scheduled_at"),
                name="task_type_status_sched_idx",
            ),
            models.Index(
                fields=("business_type", "business_key"),
                name="task_business_key_idx",
            ),
        ]
        verbose_name = "计划任务"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.get_task_type_display()} / {self.business_key}"
