from .models import UserNotification


def create_notification(
    *,
    recipient,
    category,
    event_type,
    title,
    content,
    dedupe_key,
    target_type="",
    target_id="",
    target_title="",
    action_text="",
    action_url="",
):
    notification, _ = UserNotification.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={
            "recipient": recipient,
            "category": category,
            "event_type": event_type,
            "title": title[:100],
            "content": content[:500],
            "target_type": target_type,
            "target_id": target_id,
            "target_title": target_title[:160],
            "action_text": action_text[:32],
            "action_url": action_url[:255],
        },
    )
    return notification


def create_order_notification(
    *, order, event_type, title, content, dedupe_suffix=""
):
    suffix = f":{dedupe_suffix}" if dedupe_suffix else ""
    service_name = getattr(order, "service_name_snapshot", "")
    return create_notification(
        recipient=order.customer,
        category=UserNotification.Category.ORDER,
        event_type=event_type,
        title=title,
        content=content,
        target_type="provider_order",
        target_id=order.order_no,
        target_title=f"订单 {order.order_no}{f' · {service_name}' if service_name else ''}",
        action_text="查看订单",
        action_url=f"/pages/orders/detail?orderNo={order.order_no}",
        dedupe_key=f"provider-order:{order.order_no}:{event_type}{suffix}",
    )


def create_fulfillment_review_notification(order, *, resolved=False):
    # Same event family, separate revisions/results. Internal audit notes stay private.
    suffix = "resolved" if resolved else "held"
    for recipient, parameter in ((order.customer, "orderNo"), (order.provider.user, "order_no")):
        create_notification(
            recipient=recipient, category=UserNotification.Category.ORDER,
            event_type=UserNotification.EventType.ORDER_FULFILLMENT_HELD,
            title="订单履约核查已完成" if resolved else "订单履约需客服核实",
            content=("客服已完成核查，订单恢复正常处理流程，请查看订单最新状态。" if resolved
                     else "本次履约时间需客服核实，自动确认与分账已暂停，请查看订单或联系客服。"),
            target_type="provider_order", target_id=order.order_no,
            action_text="查看订单", action_url=f"/pages/orders/detail?{parameter}={order.order_no}",
            dedupe_key=f"fulfillment-review:{order.order_no}:{order.fulfillment_revision}:{suffix}:{recipient.pk}",
        )


def create_provider_new_order_notification(*, order):
    service_name = getattr(order, "service_name_snapshot", "")
    return create_notification(
        recipient=order.provider.user,
        category=UserNotification.Category.ORDER,
        event_type=UserNotification.EventType.PROVIDER_NEW_ORDER,
        title="收到新的待接订单",
        content="用户已完成支付，请在接单时限内确认是否接单。",
        target_type="provider_order",
        target_id=order.order_no,
        target_title=f"订单 {order.order_no}{f' · {service_name}' if service_name else ''}",
        action_text="立即处理",
        action_url=f"/pages/orders/detail?order_no={order.order_no}",
        dedupe_key=f"provider-order:{order.order_no}:provider-new-order",
    )


def create_activity_notification(
    *, activity, recipient, event_type, title, content, dedupe_suffix="",
    action_text="查看活动", action_url=None,
):
    suffix = f":{dedupe_suffix}" if dedupe_suffix else ""
    return create_notification(
        recipient=recipient,
        category=UserNotification.Category.ACTIVITY,
        event_type=event_type,
        title=title,
        content=content,
        target_type="activity",
        target_id=str(activity.pk),
        target_title=activity.title,
        action_text=action_text,
        action_url=action_url or f"/pages/activities/detail?id={activity.pk}",
        dedupe_key=(
            f"activity:{activity.pk}:{event_type}:recipient:{recipient.pk}{suffix}"
        ),
    )


def create_activity_notifications(
    *, activity, recipients, event_type, title, content, dedupe_suffix=""
):
    notifications = []
    seen_recipient_ids = set()
    for recipient in recipients:
        if recipient.pk in seen_recipient_ids:
            continue
        seen_recipient_ids.add(recipient.pk)
        notifications.append(
            create_activity_notification(
                activity=activity,
                recipient=recipient,
                event_type=event_type,
                title=title,
                content=content,
                dedupe_suffix=dedupe_suffix,
            )
        )
    return notifications


def create_system_notification(
    *, recipient, event_type, title, content, dedupe_key, action_text="", action_url=""
):
    return create_notification(
        recipient=recipient,
        category=UserNotification.Category.SYSTEM,
        event_type=event_type,
        title=title,
        content=content,
        action_text=action_text,
        action_url=action_url,
        dedupe_key=dedupe_key,
    )
