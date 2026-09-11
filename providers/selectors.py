from django.db.models import Prefetch

from .models import ProviderProfile, ProviderService


def public_providers():
    active_services = ProviderService.objects.filter(
        is_active=True,
        category__is_active=True,
    ).select_related("category")
    return (
        ProviderProfile.objects.filter(
            status=ProviderProfile.Status.APPROVED,
            identity_status=ProviderProfile.IdentityStatus.VERIFIED,
            lifestyle_photo__isnull=False,
            user__is_active=True,
            user__account_status="active",
            services__is_active=True,
            services__category__is_active=True,
        )
        .exclude(bio="")
        .exclude(service_city_code="")
        .exclude(service_city_name="")
        .select_related("user", "lifestyle_photo", "live_location")
        .prefetch_related(Prefetch("services", queryset=active_services))
        .distinct()
    )
