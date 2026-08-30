from django.db import DatabaseError


DEFAULT_PLATFORM_OPERATION_RULES = {
    "provider_order_payment_timeout_minutes": 15,
    "provider_order_confirmation_timeout_days": 3,
    "activity_payment_timeout_minutes": 30,
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
