import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


def generate_support_case_no():
    return f"SC{uuid.uuid4().hex[:20].upper()}"


class SupportCase(models.Model):
    class CaseType(models.TextChoices):
        CONSULTATION = "consultation", "咨询"
        COMPLAINT = "complaint", "投诉"
        REPORT = "report", "举报"

    class TargetType(models.TextChoices):
        GENERAL = "general", "平台服务"
        PROVIDER = "provider", "达人"
        PROVIDER_ORDER = "provider_order", "达人订单"
        ACTIVITY = "activity", "活动"
        REVIEW = "review", "用户评价"

    class Reason(models.TextChoices):
        SERVICE_QUALITY = "service_quality", "服务体验问题"
        FALSE_INFORMATION = "false_information", "信息不实"
        INAPPROPRIATE_CONTENT = "inappropriate_content", "内容不当"
        PRIVATE_TRANSACTION = "private_transaction", "诱导私下交易"
        SAFETY_RISK = "safety_risk", "存在安全风险"
        PAYMENT_REFUND = "payment_refund", "支付或退款问题"
        ACCOUNT_ISSUE = "account_issue", "账号问题"
        OTHER = "other", "其他问题"

    class Status(models.TextChoices):
        PENDING = "pending", "待受理"
        PROCESSING = "processing", "处理中"
        REVIEWING = "reviewing", "复核中"
        RESOLVED = "resolved", "已处理"
        REJECTED = "rejected", "不予受理"
        CLOSED = "closed", "已关闭"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    case_no = models.CharField(
        "工单号", max_length=24, unique=True, default=generate_support_case_no,
        editable=False,
    )
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_cases",
        verbose_name="提交用户",
    )
    case_type = models.CharField("工单类型", max_length=20, choices=CaseType)
    target_type = models.CharField("关联对象", max_length=24, choices=TargetType)
    provider = models.ForeignKey(
        "providers.ProviderProfile",
        on_delete=models.PROTECT,
        related_name="support_cases",
        null=True,
        blank=True,
    )
    provider_order = models.ForeignKey(
        "orders.ProviderOrder",
        on_delete=models.PROTECT,
        related_name="support_cases",
        null=True,
        blank=True,
    )
    activity = models.ForeignKey(
        "activities.Activity",
        on_delete=models.PROTECT,
        related_name="support_cases",
        null=True,
        blank=True,
    )
    review = models.ForeignKey(
        "orders.ProviderOrderReview",
        on_delete=models.PROTECT,
        related_name="support_cases",
        null=True,
        blank=True,
    )
    reason = models.CharField("问题分类", max_length=32, choices=Reason)
    description = models.CharField("问题说明", max_length=1000)
    attachments = models.ManyToManyField(
        "mediafiles.MediaAsset",
        related_name="support_cases",
        blank=True,
        verbose_name="证据附件",
    )
    city_code = models.CharField("城市编码快照", max_length=20, blank=True)
    city_name = models.CharField("城市名称快照", max_length=50, blank=True)
    status = models.CharField(
        "处理状态", max_length=20, choices=Status, default=Status.PENDING
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_support_cases",
        null=True,
        blank=True,
        verbose_name="处理人",
    )
    result_note = models.CharField("处理结论", max_length=1000, blank=True)
    resolved_at = models.DateTimeField("完结时间", null=True, blank=True)
    review_requested_at = models.DateTimeField("申请复核时间", null=True, blank=True)
    review_reason = models.CharField("复核原因", max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "support_case"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("status", "-created_at")),
            models.Index(fields=("city_code", "status", "-created_at")),
            models.Index(fields=("reporter", "-created_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        target_type="general",
                        provider__isnull=True,
                        provider_order__isnull=True,
                        activity__isnull=True,
                        review__isnull=True,
                    )
                    | Q(
                        target_type="provider",
                        provider__isnull=False,
                        provider_order__isnull=True,
                        activity__isnull=True,
                        review__isnull=True,
                    )
                    | Q(
                        target_type="provider_order",
                        provider__isnull=True,
                        provider_order__isnull=False,
                        activity__isnull=True,
                        review__isnull=True,
                    )
                    | Q(
                        target_type="activity",
                        provider__isnull=True,
                        provider_order__isnull=True,
                        activity__isnull=False,
                        review__isnull=True,
                    )
                    | Q(
                        target_type="review",
                        provider__isnull=True,
                        provider_order__isnull=True,
                        activity__isnull=True,
                        review__isnull=False,
                    )
                ),
                name="support_case_target_matches_type",
            ),
            models.UniqueConstraint(
                fields=("reporter", "provider"),
                condition=Q(
                    provider__isnull=False,
                    status__in=("pending", "processing", "reviewing"),
                ),
                name="uniq_open_support_case_provider",
            ),
            models.UniqueConstraint(
                fields=("reporter", "provider_order"),
                condition=Q(
                    provider_order__isnull=False,
                    status__in=("pending", "processing", "reviewing"),
                ),
                name="uniq_open_support_case_order",
            ),
            models.UniqueConstraint(
                fields=("reporter", "activity"),
                condition=Q(
                    activity__isnull=False,
                    status__in=("pending", "processing", "reviewing"),
                ),
                name="uniq_open_support_case_activity",
            ),
            models.UniqueConstraint(
                fields=("reporter", "review"),
                condition=Q(
                    review__isnull=False,
                    status__in=("pending", "processing", "reviewing"),
                ),
                name="uniq_open_support_case_review",
            ),
        ]
        verbose_name = "客服工单"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.case_no} / {self.get_case_type_display()}"


class SupportCaseRecord(models.Model):
    class RecordType(models.TextChoices):
        CREATED = "created", "提交工单"
        USER_REPLY = "user_reply", "用户补充"
        OPERATOR_REPLY = "operator_reply", "客服回复"
        STATUS_CHANGED = "status_changed", "状态变更"
        REVIEW_REQUESTED = "review_requested", "申请复核"

    case = models.ForeignKey(
        SupportCase, on_delete=models.CASCADE, related_name="records"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_case_records",
        null=True,
        blank=True,
    )
    record_type = models.CharField("记录类型", max_length=24, choices=RecordType)
    content = models.CharField("记录内容", max_length=1000, blank=True)
    from_status = models.CharField("原状态", max_length=20, blank=True)
    to_status = models.CharField("新状态", max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "support_case_record"
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("case", "created_at"))]
        verbose_name = "工单处理记录"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.case.case_no} / {self.get_record_type_display()}"
