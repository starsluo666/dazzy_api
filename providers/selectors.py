from django.contrib.gis.db.models.functions import Distance
from django.db.models import ExpressionWrapper, F, FloatField, Prefetch, Value

from .models import ProviderProfile, ProviderService


def public_providers():
    active_services = ProviderService.objects.filter(
        is_active=True,
        category__is_active=True,
    ).select_related("category")
    return (
        ProviderProfile.objects.filter(
            status=ProviderProfile.Status.APPROVED,
            onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
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


def within_service_radius(queryset, point):
    """Annotate distance and keep providers able to serve the requested point."""
    radius_m = ExpressionWrapper(
        F("max_service_radius_km") * Value(1000.0),
        output_field=FloatField(),
    )
    return (
        queryset.filter(live_location__position__isnull=False)
        .annotate(distance=Distance("live_location__position", point))
        .filter(distance__lte=radius_m)
    )
