from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from orders.models import ProviderOrder

from .services import award_first_order_reward


@receiver(post_save, sender=ProviderOrder, dispatch_uid="growth_first_order_reward")
def issue_first_order_invitation_reward(sender, instance, **kwargs):
    if instance.status != ProviderOrder.Status.COMPLETED:
        return
    order_id = instance.pk
    transaction.on_commit(lambda: award_first_order_reward(order_id=order_id))
