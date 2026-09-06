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


class ProviderOrderPaymentGateway(Protocol):
    def confirm_payment(self, *, payment_no: str, amount: int) -> PaymentResult: ...

    def refund(self, *, refund_no: str, amount: int) -> RefundResult: ...


class MockProviderOrderPaymentGateway:
    """Development adapter sharing the same boundary as a formal payment channel."""

    def confirm_payment(self, *, payment_no: str, amount: int) -> PaymentResult:
        digest = sha256(f"payment:{payment_no}:{amount}".encode()).hexdigest()[:24]
        return PaymentResult(
            gateway_trade_no=f"MOCKPAY{digest.upper()}",
            paid_amount=amount,
            signature_verified=True,
        )

    def refund(self, *, refund_no: str, amount: int) -> RefundResult:
        digest = sha256(f"refund:{refund_no}:{amount}".encode()).hexdigest()[:24]
        return RefundResult(gateway_refund_no=f"MOCKREF{digest.upper()}")


def get_provider_order_payment_gateway(channel: str) -> ProviderOrderPaymentGateway:
    if channel not in ("mock_wechat", "mock_alipay"):
        raise ValueError("达人订单正式支付渠道尚未接入。")
    return MockProviderOrderPaymentGateway()
