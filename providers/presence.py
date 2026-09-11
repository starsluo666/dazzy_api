from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from .models import ProviderLiveLocation, ProviderProfile


ONLINE_TIMEOUT = timedelta(minutes=30)
ONLINE_TIMEOUT_MINUTES = 30
RECOMMENDED_REPORT_INTERVAL_SECONDS = 300
MAX_LOCATION_ACCURACY_M = Decimal("200")


def operation_rules():
    from backoffice.models import ProviderOrderingSetting
    try:
        setting = ProviderOrderingSetting.current()
        timeout_minutes = setting.location_timeout_minutes
        return {
            "timeout": timedelta(minutes=timeout_minutes) if timeout_minutes else None,
            "timeout_minutes": timeout_minutes,
            "report_interval_seconds": setting.location_report_interval_seconds,
            "max_accuracy_m": Decimal(setting.max_location_accuracy_m),
            "acceptance_timeout_minutes": setting.acceptance_timeout_minutes,
        }
    except Exception:
        return {"timeout": ONLINE_TIMEOUT, "timeout_minutes": ONLINE_TIMEOUT_MINUTES, "report_interval_seconds": RECOMMENDED_REPORT_INTERVAL_SECONDS, "max_accuracy_m": MAX_LOCATION_ACCURACY_M, "acceptance_timeout_minutes": 30}


def online_cutoff(now=None):
    timeout = operation_rules()["timeout"]
    return (now or timezone.now()) - timeout if timeout is not None else None


def online_provider_query(now=None) -> Q:
    rules = operation_rules()
    query = Q(
        status=ProviderProfile.Status.APPROVED,
        identity_status=ProviderProfile.IdentityStatus.VERIFIED,
        lifestyle_photo__isnull=False,
        is_accepting_orders=True,
        admin_order_restricted=False,
        user__is_active=True,
        user__account_status="active",
        live_location__session_id__isnull=False,
        live_location__accuracy_m__lte=rules["max_accuracy_m"],
    )
    query &= ~Q(bio="") & ~Q(service_city_code="") & ~Q(service_city_name="")
    cutoff = (now or timezone.now()) - rules["timeout"] if rules["timeout"] is not None else None
    if cutoff is not None:
        query &= Q(live_location__received_at__gte=cutoff)
    return query


def get_provider_live_location(provider: ProviderProfile) -> ProviderLiveLocation | None:
    try:
        return provider.live_location
    except ProviderLiveLocation.DoesNotExist:
        return None


def provider_is_online(provider: ProviderProfile, now=None) -> bool:
    if (
        provider.status != ProviderProfile.Status.APPROVED
        or not provider.has_verified_identity
        or not provider.is_profile_complete
        or not provider.is_accepting_orders
        or provider.admin_order_restricted
        or not provider.user.is_active
        or provider.user.account_status != provider.user.AccountStatus.ACTIVE
    ):
        return False
    location = get_provider_live_location(provider)
    rules = operation_rules()
    cutoff = (now or timezone.now()) - rules["timeout"] if rules["timeout"] is not None else None
    return bool(
        location
        and location.session_id
        and location.accuracy_m <= rules["max_accuracy_m"]
        and (cutoff is None or location.received_at >= cutoff)
    )


def location_expires_at(location: ProviderLiveLocation | None):
    timeout = operation_rules()["timeout"]
    return location.received_at + timeout if location and location.session_id and timeout else None
