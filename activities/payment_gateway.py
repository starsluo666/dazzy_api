from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol


@dataclass(frozen=True)
class PaymentResult:
    gateway_trade_no: str
    paid_amount: int
    signature_verified: bool


@dataclass(frozen=True)
class RefundResult:
    gateway_refund_no: str


class ActivityPaymentGateway(Protocol):
    def confirm_payment(self, *, order_no: str, amount: int) -> PaymentResult: ...

    def refund(self, *, refund_no: str, amount: int) -> RefundResult: ...


class MockActivityPaymentGateway:
    """Development gateway with the same result boundary as a real payment provider."""

    def confirm_payment(self, *, order_no: str, amount: int) -> PaymentResult:
        digest = sha256(f"payment:{order_no}:{amount}".encode()).hexdigest()[:24]
        return PaymentResult(
            gateway_trade_no=f"MOCKPAY{digest.upper()}",
            paid_amount=amount,
            signature_verified=True,
        )

    def refund(self, *, refund_no: str, amount: int) -> RefundResult:
        # Formal adapters must provide the same refund-no idempotency guarantee.
        digest = sha256(f"refund:{refund_no}:{amount}".encode()).hexdigest()[:24]
        return RefundResult(gateway_refund_no=f"MOCKREF{digest.upper()}")


def get_activity_payment_gateway(channel: str) -> ActivityPaymentGateway:
    if channel not in ("mock_wechat", "mock_alipay"):
        raise ValueError("正式支付渠道尚未接入。")
    return MockActivityPaymentGateway()
