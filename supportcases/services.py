from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from activities.models import Activity
from mediafiles.models import MediaAsset
from orders.models import ProviderOrder, ProviderOrderReview
from providers.models import ProviderProfile

from .models import SupportCase, SupportCaseRecord


OPEN_STATUSES = (
    SupportCase.Status.PENDING,
    SupportCase.Status.PROCESSING,
    SupportCase.Status.REVIEWING,
)


def support_case_queryset():
    return SupportCase.objects.select_related(
        "reporter", "assignee", "provider__user", "provider_order__provider",
        "activity", "review__customer",
    ).prefetch_related("attachments", "records__actor")


def _resolve_target(*, reporter, target_type, target_id):
    target_fields = {
        "provider": None,
        "provider_order": None,
        "activity": None,
        "review": None,
    }
    city_code = ""
    city_name = ""
    if target_type == SupportCase.TargetType.GENERAL:
        return target_fields, city_code, city_name
    if target_type == SupportCase.TargetType.PROVIDER:
        provider = get_object_or_404(
            ProviderProfile.objects.select_related("user"),
            user__public_id=target_id,
            status=ProviderProfile.Status.APPROVED,
        )
        if provider.user_id == reporter.id:
            raise ValidationError({"target_id": "不能投诉或举报自己。"})
        target_fields["provider"] = provider
        return target_fields, provider.service_city_code, provider.service_city_name
    if target_type == SupportCase.TargetType.PROVIDER_ORDER:
        order = get_object_or_404(
            ProviderOrder.objects.select_related("provider").select_for_update(of=("self",)),
            order_no=target_id,
        )
        if order.customer_id != reporter.id:
            raise PermissionDenied("无权反馈该订单。")
        target_fields["provider_order"] = order
        return (
            target_fields,
            order.provider.service_city_code,
            order.provider.service_city_name,
        )
    if target_type == SupportCase.TargetType.ACTIVITY:
        activity = get_object_or_404(Activity, pk=target_id)
        if activity.organizer_id == reporter.id:
            raise ValidationError({"target_id": "不能投诉或举报自己发布的活动。"})
        target_fields["activity"] = activity
        return target_fields, activity.city_code, activity.city_name
    review = get_object_or_404(
        ProviderOrderReview.objects.select_related("customer", "provider"),
        pk=target_id,
        is_visible=True,
    )
    if review.customer_id == reporter.id:
        raise ValidationError({"target_id": "不能举报自己的评价。"})
    target_fields["review"] = review
    return target_fields, review.provider.service_city_code, review.provider.service_city_name


def _validate_attachments(*, reporter, attachment_ids):
    if len(set(attachment_ids)) != len(attachment_ids):
        raise ValidationError({"attachment_ids": "请勿重复选择同一张图片。"})
    assets = list(
        MediaAsset.objects.filter(
            pk__in=attachment_ids,
            owner=reporter,
            scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.SUPPORT_ATTACHMENT,
            status=MediaAsset.Status.UPLOADED,
        )
    )
    if len(assets) != len(attachment_ids):
        raise ValidationError({"attachment_ids": "附件不存在、尚未上传或不属于当前用户。"})
    if SupportCase.objects.filter(attachments__in=assets).exists():
        raise ValidationError({"attachment_ids": "附件已用于其他工单，请重新上传。"})
    return assets


@transaction.atomic
def create_support_case(*, reporter, validated_data):
    target_fields, city_code, city_name = _resolve_target(
        reporter=reporter,
        target_type=validated_data["target_type"],
        target_id=validated_data.get("target_id", "").strip(),
    )
    attachment_ids = validated_data.get("attachment_ids", [])
    attachments = _validate_attachments(
        reporter=reporter, attachment_ids=attachment_ids
    )
    if validated_data.get("reward_eligible"):
        order = target_fields["provider_order"]
        if (order is None or order.status != ProviderOrder.Status.PENDING_REVIEW
                or not order.review_expires_at or order.review_expires_at <= timezone.now()):
            raise ValidationError({"target_id": "仅可举报处于待评价期限内的本人订单。"})
        if SupportCase.objects.filter(reporter=reporter, provider_order=order, reward_eligible=True).exists():
            raise ValidationError({"target_id": "该订单已提交过举报有奖申请。"})
    lookup = {key: value for key, value in target_fields.items() if value is not None}
    # Order feedback and reward reports can coexist. Other targets retain their
    # existing one-open-case constraint regardless of case type.
    dedupe_lookup = dict(lookup)
    if target_fields["provider_order"] is not None:
        dedupe_lookup["case_type"] = validated_data["case_type"]
    if lookup:
        existing = support_case_queryset().filter(
            reporter=reporter, status__in=OPEN_STATUSES, **dedupe_lookup
        ).first()
        if existing:
            if validated_data.get("reward_eligible") and not existing.reward_eligible:
                raise ValidationError({"target_id": "该订单已有进行中的举报，请先完成原举报。"})
            return existing, False
    try:
        with transaction.atomic():
            case = SupportCase.objects.create(
                reporter=reporter,
                case_type=validated_data["case_type"],
                reward_eligible=validated_data.get("reward_eligible", False),
                target_type=validated_data["target_type"],
                reason=validated_data["reason"],
                description=validated_data["description"],
                city_code=city_code,
                city_name=city_name,
                **target_fields,
            )
    except IntegrityError:
        if not lookup:
            raise
        case = support_case_queryset().filter(
            reporter=reporter, status__in=OPEN_STATUSES, **dedupe_lookup
        ).first()
        if case is None or (validated_data.get("reward_eligible") and not case.reward_eligible):
            message = ("该订单已经提交过举报有奖申请。" if validated_data.get("reward_eligible")
                       else "该对象已有反馈，请刷新后查看。")
            raise ValidationError({"target_id": message})
        return case, False
    if attachments:
        case.attachments.set(attachments)
    SupportCaseRecord.objects.create(
        case=case,
        actor=reporter,
        record_type=SupportCaseRecord.RecordType.CREATED,
        content=case.description,
        to_status=case.status,
    )
    return support_case_queryset().get(pk=case.pk), True


@transaction.atomic
def add_user_reply(*, case, reporter, content):
    case = SupportCase.objects.select_for_update().get(pk=case.pk)
    if case.reporter_id != reporter.id:
        raise PermissionDenied("无权操作该工单。")
    if case.status not in OPEN_STATUSES:
        raise ValidationError("该工单已完结，不能继续补充。")
    SupportCaseRecord.objects.create(
        case=case,
        actor=reporter,
        record_type=SupportCaseRecord.RecordType.USER_REPLY,
        content=content,
    )
    return support_case_queryset().get(pk=case.pk)


@transaction.atomic
def request_case_review(*, case, reporter, reason):
    case = SupportCase.objects.select_for_update().get(pk=case.pk)
    if case.reporter_id != reporter.id:
        raise PermissionDenied("无权操作该工单。")
    if case.status not in (SupportCase.Status.RESOLVED, SupportCase.Status.REJECTED):
        raise ValidationError("只有已处理或不予受理的工单可以申请复核。")
    if case.review_requested_at:
        raise ValidationError("该工单已经申请过复核。")
    previous = case.status
    case.status = SupportCase.Status.REVIEWING
    case.review_requested_at = timezone.now()
    case.review_reason = reason
    case.assignee = None
    case.resolved_at = None
    case.save(
        update_fields=(
            "status", "review_requested_at", "review_reason", "assignee",
            "resolved_at", "updated_at",
        )
    )
    SupportCaseRecord.objects.create(
        case=case,
        actor=reporter,
        record_type=SupportCaseRecord.RecordType.REVIEW_REQUESTED,
        content=reason,
        from_status=previous,
        to_status=case.status,
    )
    return support_case_queryset().get(pk=case.pk)
