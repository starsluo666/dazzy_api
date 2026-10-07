"""Shared task visibility for task lists, diagnostics and notification links."""
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from activities.models import Activity, ActivityParticipationPaymentOrder, ActivityParticipationRefundOrder, ActivityPublishOrder
from orders.models import ProviderOrder, ProviderOrderRefundOrder
from taskcenter.models import ScheduledTask


def scoped_scheduled_tasks(access):
    orders = ProviderOrder.objects.all()
    activities = Activity.objects.all()
    if not access.all_data:
        orders = orders.filter(provider__service_city_code__in=access.city_codes)
        activities = activities.filter(city_code__in=access.city_codes)
    queryset = ScheduledTask.objects.all()
    if access.all_data:
        return queryset
    visible_order_nos = orders.values("order_no")
    visible_provider_refund_nos = ProviderOrderRefundOrder.objects.filter(
        order__in=orders
    ).values("refund_no")
    visible_activities = activities
    visible_activity_ids = visible_activities.annotate(
        task_business_key=Cast("id", output_field=CharField())
    ).values("task_business_key")
    visible_participation_order_nos = ActivityParticipationPaymentOrder.objects.filter(
        participation__activity__in=visible_activities,
    ).values("order_no")
    visible_publish_order_nos = ActivityPublishOrder.objects.filter(
        activity__in=visible_activities
    ).values("order_no")
    visible_participation_refund_nos = ActivityParticipationRefundOrder.objects.filter(
        activity__in=visible_activities
    ).values("refund_no")
    return queryset.filter(
        Q(
            business_type="provider_order",
            business_key__in=visible_order_nos,
        )
        | Q(
            business_type="provider_order_refund",
            business_key__in=visible_provider_refund_nos,
        )
        | Q(
            business_type="activity",
            business_key__in=visible_activity_ids,
        )
        | Q(
            business_type="activity_participation",
            business_key__in=visible_participation_order_nos,
        )
        | Q(
            business_type="activity_publish_payment",
            business_key__in=visible_publish_order_nos,
        )
        | Q(
            business_type="activity_participation_refund",
            business_key__in=visible_participation_refund_nos,
        )
    )
