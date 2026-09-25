from django.db import transaction
from django.utils import timezone

from accounts.models import User
from notifications.models import UserNotification
from notifications.services import create_notification
from orders.coupons import issue_coupon
from orders.models import ProviderOrder

from .models import GrowthCampaignConfig, Invitation, NewcomerGiftGrant


def coupon_template_payload(template):
    if template is None:
        return None
    return {
        "public_id": str(template.public_id),
        "name": template.name,
        "description": template.description,
        "face_amount": template.face_amount,
        "min_order_amount": template.min_order_amount,
        "valid_days": template.valid_days,
        "is_active": template.is_active,
    }


def get_campaign_config(*, lock=False):
    queryset = GrowthCampaignConfig.objects
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    return (
        queryset.select_related(
            "registration_reward_template",
            "first_order_reward_template",
        )
        .prefetch_related("newcomer_gift_items__template")
        .filter(pk=1)
        .first()
    )


def campaign_payload(config):
    if config is None:
        return {
            "newcomer_gift_enabled": False,
            "invitation_enabled": False,
            "newcomer_gift_templates": [],
            "registration_reward_template": None,
            "first_order_reward_template": None,
            "updated_at": None,
        }
    return {
        "newcomer_gift_enabled": config.newcomer_gift_enabled,
        "invitation_enabled": config.invitation_enabled,
        "newcomer_gift_templates": [
            coupon_template_payload(item.template)
            for item in config.newcomer_gift_items.all()
            if item.template.is_active
        ],
        "registration_reward_template": coupon_template_payload(
            config.registration_reward_template
        ),
        "first_order_reward_template": coupon_template_payload(
            config.first_order_reward_template
        ),
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


def _active_template(template):
    return template if template is not None and template.is_active else None


@transaction.atomic
def register_invited_user(*, user, invite_code):
    """Bind one valid inviter and issue all registration-time rewards once."""
    if not invite_code:
        return None
    config = get_campaign_config(lock=True)
    if config is None or not config.invitation_enabled:
        return None
    inviter = (
        User.objects.filter(
            public_id=invite_code,
            account_status=User.AccountStatus.ACTIVE,
            is_active=True,
        )
        .exclude(pk=user.pk)
        .first()
    )
    if inviter is None:
        return None

    invitation, created = Invitation.objects.get_or_create(
        invitee=user,
        defaults={
            "inviter": inviter,
            "invite_code_snapshot": str(invite_code),
            "registered_at": timezone.now(),
        },
    )
    if not created:
        return invitation

    registration_template = _active_template(config.registration_reward_template)
    if registration_template:
        invitation.registration_reward_coupon = issue_coupon(
            owner=inviter,
            source="invite_registration",
            template=registration_template,
        )
        invitation.save(update_fields=("registration_reward_coupon", "updated_at"))

    if config.newcomer_gift_enabled:
        gift_items = [
            item for item in config.newcomer_gift_items.all() if item.template.is_active
        ]
        snapshot = [coupon_template_payload(item.template) for item in gift_items]
        coupons = [
            issue_coupon(
                owner=user,
                source="newcomer_gift",
                template=item.template,
                notify=False,
            )
            for item in gift_items
        ]
        if coupons:
            grant = NewcomerGiftGrant.objects.create(
                recipient=user,
                invitation=invitation,
                template_snapshot=snapshot,
            )
            grant.coupons.add(*coupons)
            create_notification(
                recipient=user,
                category=UserNotification.Category.SYSTEM,
                event_type=UserNotification.EventType.COUPON_ISSUED,
                title="新人礼包已到账",
                content=f"{len(coupons)} 张新人优惠券已放入你的账户，快去看看吧。",
                target_type="newcomer_gift",
                target_id=str(invitation.public_id),
                target_title="新人礼包",
                action_text="查看礼包",
                action_url="/pages/invitations/newcomer",
                dedupe_key=f"newcomer-gift:{user.public_id}",
            )
    return invitation


@transaction.atomic
def award_first_order_reward(*, order_id):
    order = ProviderOrder.objects.select_related("customer").filter(pk=order_id).first()
    if order is None or order.status != ProviderOrder.Status.COMPLETED:
        return None
    invitation = (
        Invitation.objects.select_for_update()
        .select_related("inviter")
        .filter(invitee=order.customer)
        .first()
    )
    if invitation is None or invitation.first_order_reward_coupon_id:
        return invitation
    config = get_campaign_config(lock=True)
    template = _active_template(config.first_order_reward_template) if config else None
    if config is None or not config.invitation_enabled or template is None:
        return invitation
    if (
        invitation.inviter.account_status != User.AccountStatus.ACTIVE
        or not invitation.inviter.is_active
    ):
        return invitation
    coupon = issue_coupon(
        owner=invitation.inviter,
        source="invite_first_order",
        template=template,
    )
    invitation.first_order_reward_coupon = coupon
    invitation.first_completed_order = order
    invitation.first_order_completed_at = timezone.now()
    invitation.save(
        update_fields=(
            "first_order_reward_coupon",
            "first_completed_order",
            "first_order_completed_at",
            "updated_at",
        )
    )
    return invitation


def viewer_campaign_status(user):
    if not user or not user.is_authenticated:
        return {"status": "login_required", "received_coupon_count": 0}
    invitation = Invitation.objects.filter(invitee=user).first()
    grant = (
        NewcomerGiftGrant.objects.filter(recipient=user)
        .prefetch_related("coupons")
        .first()
    )
    if grant:
        return {
            "status": "received",
            "received_coupon_count": grant.coupons.count(),
            "granted_at": grant.granted_at,
        }
    return {
        "status": "pending" if invitation else "not_eligible",
        "received_coupon_count": 0,
    }
