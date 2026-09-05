def calculate_publish_service_fee(principal_amount: int) -> int:
    """Calculate 10% in cents using explicit round-half-up semantics."""
    return (principal_amount + 5) // 10
