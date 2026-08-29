from django.db.models import Prefetch

from .models import ProviderProfile, ProviderService


def public_providers():
    active_services = ProviderService.objects.filter(is_active=True).select_related("category")
    return (
        ProviderProfile.objects.filter(
            status=ProviderProfile.Status.APPROVED,
            user__is_active=True,
            user__account_status="active",
            services__is_active=True,
        )
        .select_related("user", "lifestyle_photo", "live_location")
        .prefetch_related(Prefetch("services", queryset=active_services))
        .distinct()
    )
