from decimal import Decimal, ROUND_HALF_UP


DEFAULT_ACTIVITY_SERVICE_FEE_RATE = Decimal("0.1000")


def calculate_activity_service_fee(
    principal_amount: int,
    service_fee_rate=DEFAULT_ACTIVITY_SERVICE_FEE_RATE,
) -> int:
    """Calculate the fee in cents using the activity's snapshotted rate."""
    rate = Decimal(str(service_fee_rate))
    return int((Decimal(principal_amount) * rate).quantize(Decimal("1"), ROUND_HALF_UP))


def calculate_publish_service_fee(
    principal_amount: int,
    service_fee_rate=DEFAULT_ACTIVITY_SERVICE_FEE_RATE,
) -> int:
    """Backward-compatible alias used by publish and participation pricing."""
    return calculate_activity_service_fee(principal_amount, service_fee_rate)
