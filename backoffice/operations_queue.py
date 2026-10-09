"""Permission-scoped, live work queues. Reading/acknowledging never changes business state.

The inbox projects unresolved business records, not copies of their lifecycle. Read
receipts are per operator and event revision; a new revision becomes unread again.
All pagination/counts/filter links use these same querysets. No channel calls.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Case, CharField, Count, Exists, F, Min, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, Concat
from django.utils import timezone
from rest_framework.exceptions import NotFound

from activities.models import Activity, ActivityAfterSalesCase, ActivityParticipationRefundOrder, ActivityReport
from orders.models import ProviderOrder, ProviderOrderRefundOrder
from orders.timeouts import legacy_overdue_query
from providers.models import ProviderProfile, ProviderProfileRevision, ProviderServiceRevision
from supportcases.models import SupportCase, SupportCaseRecord
from taskcenter.models import ScheduledTask
from .models import AdminWorkReadReceipt, ProviderOrderAfterSalesCase
from .finance_work import finance_queues
from .task_scope import scoped_scheduled_tasks


@dataclass
class Queue:
    key: str
    label: str
    group: str
    queryset: object
    page: str
    reference: str
    time_field: object = "created_at"
    version: object = "status"
    hours: int = 24
    query: dict | None = None
    sort_field: str = "updated_at"


def permitted(access, *permissions):
    return "*" in access.permissions or all(p in access.permissions for p in permissions)


def scope(queryset, access, field):
    return queryset if access.all_data else queryset.filter(**{f"{field}__in": access.city_codes})


def refund_attention(queryset, task_type, now):
    failed_tasks = ScheduledTask.objects.filter(task_type=task_type, status="failed").values("business_key")
    failed_at = ScheduledTask.objects.filter(task_type=task_type, status="failed", business_key=OuterRef("refund_no")).order_by("-finished_at").values("finished_at")[:1]
    return queryset.exclude(status="succeeded").filter(
        Q(status="failed") | Q(refund_no__in=failed_tasks)
        | Q(status__in=("pending", "processing"), created_at__lte=now - timedelta(hours=24))
    ).annotate(_refund_event=Concat("status", Value(":"), Coalesce(Cast(Subquery(failed_at), CharField()), Value("waiting"))))


def queues(access, *, now=None):
    now = now or timezone.now()
    result = []
    providers = scope(ProviderProfile.objects.all(), access, "service_city_code")
    orders = scope(ProviderOrder.objects.all(), access, "provider__service_city_code")
    activities = scope(Activity.objects.all(), access, "city_code")
    if permitted(access, "provider.review"):
        result.extend([
            Queue("provider_application_review", "达人入驻初审", "review", providers.filter(status="pending"),
                  "provider_reviews", "display_name", Coalesce("submitted_at", "created_at"),
                  Coalesce(Cast("submitted_at", CharField()), Value("initial")), query={"review": "application"}),
            Queue("provider_onboarding_review", "达人开通审核", "review", providers.filter(onboarding_status="pending_review"),
                  "provider_reviews", "display_name", Coalesce("onboarding_submitted_at", "created_at"),
                  Coalesce(Cast("onboarding_submitted_at", CharField()), Value("initial")), query={"review": "onboarding"}),
        ])
        for key, label, model, mode in [
            ("provider_profile_review", "达人资料变更审核", ProviderProfileRevision, "profile"),
            ("provider_service_review", "达人服务变更审核", ProviderServiceRevision, "service"),
        ]:
            result.append(Queue(key, label, "review", model.objects.filter(provider__in=providers,
                provider__onboarding_status="approved", status="pending"), "provider_reviews", "provider__display_name",
                "submitted_at", query={"review": mode}))
    if permitted(access, "activity.view", "activity.review"):
        result.append(Queue("activity_review", "活动发布审核", "review", activities.filter(status="pending_review"),
                            "activities", "title", version=Cast("updated_at", CharField()), query={"activity_status": "pending_review"}))
    if permitted(access, "order.fulfillment.view", "order.fulfillment.review"):
        active = orders.exclude(status__in=("cancelled", "refunded", "terminated"))
        result.append(Queue("fulfillment_review", "履约异常待核查", "urgent", active.filter(fulfillment_review_required=True),
                            "orders", "order_no", Coalesce("completion_submitted_at", "service_started_at", "start_deadline_at", "starts_at"),
                            Cast("fulfillment_revision", CharField()), hours=1))
        result.append(Queue("legacy_overdue", "历史超时待核查", "support",
                            active.filter(legacy_overdue_query(now=now), fulfillment_review_required=False),
                            "orders", "order_no", "starts_at", hours=1))
    if permitted(access, "order.fulfillment.view", "order.support_note.add"):
        result.append(Queue("order_support", "订单转客服处理", "support",
                            orders.filter(status="pending_support"),
                            "orders", "order_no", Coalesce("provider_rejected_at", "paid_at", "created_at"), hours=1))
    if permitted(access, "order.finance.view", "order.finance.manage"):
        refunds = refund_attention(ProviderOrderRefundOrder.objects.filter(order__in=orders),
                                   ScheduledTask.Type.PROVIDER_ORDER_REFUND, now)
        result.append(Queue("provider_refund_attention", "达人订单退款异常", "urgent", refunds,
                            "settlements", "refund_no", version=F("_refund_event"), hours=1, query={"record_type": "refund"}))
    if permitted(access, "order.finance.view", "order.fulfillment.view"):
        result.append(Queue("cancellation_reconciliation", "取消订单剩余款待核账", "finance",
            orders.filter(cancellation_record__retained_amount__gt=0).exclude(settlement__status__in=("settled", "cancelled")),
            "orders", "order_no", "cancelled_at", version=Cast("cancelled_at", CharField())))
    if permitted(access, "order.after_sales.view", "order.after_sales.review"):
        cases = ProviderOrderAfterSalesCase.objects.filter(order__in=orders, status__in=("pending", "processing"))
        result.append(Queue("provider_after_sales", "达人订单售后待处理", "support",
                            cases.filter(requires_supervisor=False), "after_sales", "case_no", hours=4))
        if permitted(access, "refund.supervise"):
            result.append(Queue("provider_refund_escalated", "达人退款待主管审核", "urgent",
                cases.filter(requires_supervisor=True), "after_sales", "case_no", hours=1))
    if permitted(access, "order.after_sales.view", "order.finance.view"):
        result.append(Queue("termination_reconciliation", "提前终止剩余款待核账", "finance",
            ProviderOrderAfterSalesCase.objects.filter(order__in=orders, case_type="early_termination",
                status__in=("refunded", "resolved"), order__status="terminated").filter(
                    Q(order__settlement__status="dispute_frozen") | Q(order__settlement__isnull=True)),
            "after_sales", "case_no", hours=24))
    if permitted(access, "support.case.view", "support.case.manage"):
        # Replies and review requests must alert even if the number of cases is unchanged.
        latest = SupportCaseRecord.objects.filter(case_id=OuterRef("pk"),
            record_type__in=("user_reply", "review_requested")).order_by("-id").values("id")[:1]
        cases = scope(SupportCase.objects.filter(status__in=("pending", "processing", "reviewing")), access, "city_code")
        cases = cases.annotate(_reply=Coalesce(Cast(Subquery(latest), CharField()), Value("0")))
        result.append(Queue("support_cases", "客服投诉与举报", "support", cases, "support_cases", "case_no",
                            version=Concat("status", Value(":"), F("_reply")), hours=4))
    if permitted(access, "activity_report.view", "activity_report.manage"):
        result.append(Queue("activity_reports", "活动举报待处理", "support",
                            ActivityReport.objects.filter(activity__in=activities, status__in=("pending", "processing")),
                            "activity_reports", "case_no", hours=4))
    if permitted(access, "activity_finance.view", "activity_after_sales.manage"):
        cases = ActivityAfterSalesCase.objects.filter(participation__activity__in=activities, status__in=("pending", "processing"))
        result.append(Queue("activity_after_sales", "活动售后待处理", "support",
                            cases.filter(requires_supervisor=False),
                            "activity_finance", "case_no", hours=4, query={"record_type": "after_sales"}))
        if permitted(access, "refund.supervise"):
            result.append(Queue("activity_refund_escalated", "活动退款待主管审核", "urgent",
                cases.filter(requires_supervisor=True), "activity_finance", "case_no", hours=1, query={"record_type": "after_sales"}))
        refunds = refund_attention(ActivityParticipationRefundOrder.objects.filter(activity__in=activities),
                                   ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND, now)
        result.append(Queue("activity_refund_attention", "活动报名退款异常", "urgent", refunds,
                            "activity_finance", "refund_no", version=F("_refund_event"), hours=1, query={"record_type": "refund"}))
    if permitted(access, "order.finance.view", "order.finance.manage"):
        result.extend(finance_queues(providers, now, Queue))
    if permitted(access, "system.task.view"):
        # Exclude non-critical reminders/review defaults; all monetary and lifecycle tasks are included.
        tasks = scoped_scheduled_tasks(access).exclude(task_type__in=(
            ScheduledTask.Type.PROVIDER_DEPARTURE_REMINDER, ScheduledTask.Type.PROVIDER_ORDER_REVIEW_TIMEOUT,
        )).filter(
            Q(status="failed") | Q(status="pending", available_at__lte=now - timedelta(minutes=5))
            | Q(status="running", started_at__lte=now - timedelta(minutes=5))
            | Q(status="running", started_at__isnull=True, updated_at__lte=now - timedelta(minutes=5))
        )
        result.append(Queue("critical_tasks", "关键任务失败或超时", "system", tasks, "tasks", "business_key",
            Case(When(status="failed", then=Coalesce("finished_at", "updated_at")),
                 When(status="running", then=Coalesce("started_at", "updated_at")), default=F("available_at")),
            Concat("status", Value(":"), Cast("attempt_count", CharField()), Value(":"),
                   Coalesce(Cast("finished_at", CharField()), Cast("available_at", CharField()))), hours=1))
    return result


def queue_queryset(queue, user, now):
    timestamp = F(queue.time_field) if isinstance(queue.time_field, str) else queue.time_field
    version = F(queue.version) if isinstance(queue.version, str) else queue.version
    qs = queue.queryset.annotate(_work_id=Cast("pk", CharField()), _work_at=timestamp,
        _version=Cast(version, CharField()))
    qs = qs.annotate(_overdue=Case(When(_work_at__lte=now - timedelta(hours=queue.hours), then=Value(True)), default=Value(False)))
    qs = qs.annotate(_event=Concat("_version", Case(When(_overdue=True, then=Value(":overdue")), default=Value(":open"))))
    return qs.annotate(_read=Exists(AdminWorkReadReceipt.objects.filter(
        user=user, queue_key=queue.key, object_id=OuterRef("_work_id"), event_version=OuterRef("_event"))))


def target(queue, reference=None, object_id=None):
    query = {"todo": queue.key, **(queue.query or {})}
    if reference:
        query["search"] = reference
    if object_id:
        query["work_id"] = object_id
    return {"page": queue.page, "query": query}


def work_summary(access, user, *, now=None):
    now = now or timezone.now()
    todos = []
    for queue in queues(access, now=now):
        qs = queue_queryset(queue, user, now)
        stats = qs.aggregate(count=Count("pk"),
            unread_count=Count("pk", filter=Q(_read=False)), overdue_count=Count("pk", filter=Q(_overdue=True)), oldest_at=Min("_work_at"))
        todos.append({"key": queue.key, "label": queue.label, "group": queue.group,
            "priority": "high" if queue.group in ("urgent", "finance", "system") or stats["overdue_count"] else "medium",
            **stats, "target": target(queue), "reminder_hours": queue.hours,
            "signals": [f"{queue.key}:{item['_work_id']}:{item['_event']}" for item in
                        qs.filter(_read=False).order_by(f"-{queue.sort_field}", "-pk").values("_work_id", "_event")[:5]]})
    return {"todos": todos, "total": sum(t["count"] for t in todos),
            "unread_count": sum(t["unread_count"] for t in todos),
            "overdue_count": sum(t["overdue_count"] for t in todos), "updated_at": now,
            "viewer": str(user.public_id)}


def get_queue(access, key):
    queue = next((q for q in queues(access) if q.key == key), None)
    if queue is None:
        raise NotFound("待办不存在或当前账号无处理权限。")
    return queue


def filter_work_queue(queryset, access, params, allowed):
    key = params.get("todo")
    if not key:
        return queryset
    if key not in allowed:
        raise NotFound("此页面不支持该待办。")
    queue = get_queue(access, key)
    if queue.queryset.model != queryset.model:
        raise NotFound("待办类型不匹配。")
    queryset = queryset.filter(pk__in=queue.queryset.values("pk"))
    if object_id := params.get("work_id"):
        if not object_id.isascii() or not object_id.isdigit() or len(object_id) > 18:
            raise NotFound("无效的待办编号。")
        queryset = queryset.filter(pk=object_id)
    return queryset
