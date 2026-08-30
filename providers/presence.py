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
        return {
            "timeout": timedelta(minutes=setting.location_timeout_minutes),
            "timeout_minutes": setting.location_timeout_minutes,
            "report_interval_seconds": setting.location_report_interval_seconds,
            "max_accuracy_m": Decimal(setting.max_location_accuracy_m),
            "acceptance_timeout_minutes": setting.acceptance_timeout_minutes,
        }
    except Exception:
        return {"timeout": ONLINE_TIMEOUT, "timeout_minutes": ONLINE_TIMEOUT_MINUTES, "report_interval_seconds": RECOMMENDED_REPORT_INTERVAL_SECONDS, "max_accuracy_m": MAX_LOCATION_ACCURACY_M, "acceptance_timeout_minutes": 30}


def online_cutoff(now=None):
    return (now or timezone.now()) - operation_rules()["timeout"]


def online_provider_query(now=None) -> Q:
    return Q(
        status=ProviderProfile.Status.APPROVED,
        is_accepting_orders=True,
        admin_order_restricted=False,
        user__is_active=True,
        user__account_status="active",
        live_location__session_id__isnull=False,
        live_location__received_at__gte=online_cutoff(now),
        live_location__accuracy_m__lte=operation_rules()["max_accuracy_m"],
    )


def get_provider_live_location(provider: ProviderProfile) -> ProviderLiveLocation | None:
    try:
        return provider.live_location
    except ProviderLiveLocation.DoesNotExist:
        return None


def provider_is_online(provider: ProviderProfile, now=None) -> bool:
    if (
        provider.status != ProviderProfile.Status.APPROVED
        or not provider.is_accepting_orders
        or provider.admin_order_restricted
        or not provider.user.is_active
        or provider.user.account_status != provider.user.AccountStatus.ACTIVE
    ):
        return False
    location = get_provider_live_location(provider)
    return bool(
        location
        and location.session_id
        and location.accuracy_m <= operation_rules()["max_accuracy_m"]
        and location.received_at >= online_cutoff(now)
    )


def location_expires_at(location: ProviderLiveLocation | None):
    return location.received_at + operation_rules()["timeout"] if location and location.session_id else None
