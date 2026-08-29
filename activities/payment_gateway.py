from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True)
class PaymentResult:
    gateway_trade_no: str


@dataclass(frozen=True)
class RefundResult:
    gateway_refund_no: str


class ActivityPaymentGateway(Protocol):
    def confirm_payment(self, *, order_no: str, amount: int) -> PaymentResult: ...

    def refund(self, *, refund_no: str, amount: int) -> RefundResult: ...


class MockActivityPaymentGateway:
    """Development gateway with the same result boundary as a real payment provider."""

    def confirm_payment(self, *, order_no: str, amount: int) -> PaymentResult:
        return PaymentResult(gateway_trade_no=f"MOCKPAY{uuid4().hex[:24].upper()}")

    def refund(self, *, refund_no: str, amount: int) -> RefundResult:
        return RefundResult(gateway_refund_no=f"MOCKREF{uuid4().hex[:24].upper()}")


def get_activity_payment_gateway(channel: str) -> ActivityPaymentGateway:
    if channel not in ("mock_wechat", "mock_alipay"):
        raise ValueError("正式支付渠道尚未接入。")
    return MockActivityPaymentGateway()
