import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .invite_reward_payouts import query_reward, submit_reward
from .models import ProviderInvitePayout, ProviderInviteReward
from orders.distribution_transport import DistributionPreflightError
from providers.models import ProviderProfile
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


@shared_task(name="growth.process_invite_reward_payouts", ignore_result=True)
def process_invite_reward_payouts():
    now = timezone.now()
    recorded = ProviderInvitePayout.objects.filter(
        funds_released=False, status__in=("submitting", "processing", "unknown"),
        created_at__lte=now - timedelta(minutes=2),
    ).order_by(F("last_queried_at").asc(nulls_first=True), "pk")[:20]
    for payout in recorded:
        try:
            query_reward(payout)
        except Exception:
            ProviderInvitePayout.objects.filter(pk=payout.pk).update(last_queried_at=now)
            logger.warning("Invite reward payout query deferred: %s", payout.req_seq_id)

    if not settings.HUIFU_INVITE_REWARD_PAYOUT_ENABLED:
        return
    approved = ProviderInviteReward.objects.filter(
        status="approved", payout_method="balance", payout__isnull=True,
    ).order_by(F("payout_checked_at").asc(nulls_first=True), "pk")[:20]
    for reward in approved:
        reason = ""
        try:
            submit_reward(reward.pk)
        except ProviderProfile.DoesNotExist:
            reason = "邀请方未绑定可用的收款账号。"
        except (ValidationError, DistributionPreflightError):
            reason = "收款账户、订单资格或平台资金尚未通过核验。"
        except Exception:
            reason = "付款预检查暂未完成，请核对收款和资金配置。"
            logger.warning("Invite reward payout preflight deferred: %s", reward.public_id)
        ProviderInviteReward.objects.filter(
            pk=reward.pk, status="approved", payout__isnull=True,
        ).update(payout_checked_at=now, payout_blocker=reason)
