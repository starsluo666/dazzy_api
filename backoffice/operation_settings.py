from django.db import DatabaseError
from decimal import Decimal


DEFAULT_PLATFORM_OPERATION_RULES = {
    "customer_service_phone": "",
    "provider_order_payment_timeout_minutes": 15,
    "provider_order_confirmation_timeout_days": 3,
    "provider_order_settlement_freeze_days": 1,
    "activity_payment_timeout_minutes": 30,
    "activity_service_fee_rate": Decimal("0.1000"),
    "activity_min_capacity": 2,
    "activity_max_capacity": 100,
    "activity_min_aa_principal_amount": 1,
    "activity_max_aa_principal_amount": 10_000_000,
    "activity_minimum_advance_hours": 48,
    "activity_maximum_advance_days": 30,
    "activity_settlement_confirmation_hours": 24,
    "activity_settlement_risk_freeze_days": 7,
}


def platform_operation_rules():
    from .models import PlatformOperationSetting

    try:
        setting = PlatformOperationSetting.current()
    except DatabaseError:
        return DEFAULT_PLATFORM_OPERATION_RULES.copy()
    return {
        key: getattr(setting, key)
        for key in DEFAULT_PLATFORM_OPERATION_RULES
    }
