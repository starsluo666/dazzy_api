"""Coupon campaign read model and atomic, once-per-user redemption."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from mediafiles.services import build_media_url
from .coupon_batches import eligible_recipients, template_snapshot
from .coupons import coupon_payload, issue_coupon
from .models import CouponCampaign, CouponCampaignClaim


def campaign_state(campaign, now=None):
    now = now or timezone.now()
    if campaign.status != "published":
        return campaign.status
    if now >= campaign.ends_at:
        return "ended"
    if now < campaign.starts_at:
        return "upcoming"
    if campaign.issued_count >= campaign.stock:
        return "exhausted"
    return "active"


def campaign_payload(campaign, *, claim=None, admin=False):
    state = campaign_state(campaign)
    data = {
        "public_id": str(campaign.public_id), "name": campaign.name,
        "banner_url": build_media_url(campaign.banner.object_key),
        "coupon": campaign.template_snapshot or template_snapshot(campaign.template),
        "starts_at": campaign.starts_at, "ends_at": campaign.ends_at,
        "state": state, "claimed": claim is not None,
        "user_coupon": coupon_payload(claim.coupon) if claim else None,
        "can_claim": state == "active" and claim is None,
    }
    if admin:
        data.update(
            status=campaign.status, banner_id=str(campaign.banner_id),
            template_public_id=str(campaign.template.public_id),
            stock=campaign.stock, issued_count=campaign.issued_count,
            remaining_count=campaign.stock - campaign.issued_count,
            used_count=campaign.claims.filter(coupon__status="used").count(),
            sort_order=campaign.sort_order, revision=campaign.revision,
            published_at=campaign.published_at, updated_at=campaign.updated_at,
        )
    return data


def home_coupon_campaigns(user):
    # Do not advertise future, finished, offline or exhausted offers on the home.
    # Their direct details still explain the state to users with an open sheet.
    now = timezone.now()
    from django.db.models import F
    campaigns = list(CouponCampaign.objects.select_related("banner", "template").filter(
        status="published", starts_at__lte=now, ends_at__gt=now, issued_count__lt=F("stock"),
    )[:20])
    claims = {}
    if user.is_authenticated:
        claims = {c.campaign_id: c for c in CouponCampaignClaim.objects.select_related("coupon__template").filter(
            user=user, campaign__in=campaigns,
        )}
    return [campaign_payload(c, claim=claims.get(c.pk)) for c in campaigns]


@transaction.atomic
def claim_campaign(*, campaign_id, user):
    # Campaign lock serializes claims, stock edits and publication changes.
    # Unique(campaign,user) and stock constraints are a second database guard.
    campaign = get_object_or_404(CouponCampaign.objects.select_for_update(), public_id=campaign_id)
    if not eligible_recipients().filter(pk=user.pk).exists():
        raise ValidationError("当前账号不能领取用户活动优惠券。")
    existing = campaign.claims.select_related("coupon__template").filter(user=user).first()
    if existing:
        return campaign_payload(campaign, claim=existing)
    state = campaign_state(campaign)
    if state != "active":
        raise ValidationError({"detail": {
            "draft": "活动尚未发布。", "offline": "活动已下架。", "upcoming": "活动尚未开始。",
            "ended": "活动已结束。", "exhausted": "优惠券已领完。",
        }[state]})
    coupon = issue_coupon(owner=user, source="campaign", template=campaign.template,
                          template_snapshot=campaign.template_snapshot)
    claim = CouponCampaignClaim.objects.create(campaign=campaign, user=user, coupon=coupon)
    campaign.issued_count += 1
    campaign.save(update_fields=("issued_count",))
    return campaign_payload(campaign, claim=claim)
