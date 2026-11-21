from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import ProviderOrder


def blocking_orders(provider, starts_at, ends_at, *, now=None):
    status = ProviderOrder.Status
    active = Q(status=status.PENDING_PAYMENT, payment_expires_at__gt=now or timezone.now()) | Q(
        status__in=(
            status.PENDING_ACCEPTANCE, status.PENDING_SUPPORT, status.PENDING_SERVICE,
            status.DEPARTED, status.IN_SERVICE, status.PENDING_CONFIRMATION, status.AFTER_SALES,
        )
    ) | Q(
        status__in=(status.PENDING_REVIEW, status.COMPLETED), renewal_travel_until__isnull=False
    )
    return (
        ProviderOrder.objects.filter(active, provider=provider, starts_at__lt=ends_at)
        .annotate(occupied_until=Coalesce("renewal_travel_until", "ends_at"))
        .filter(occupied_until__gt=starts_at)
    )
