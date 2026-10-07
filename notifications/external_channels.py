"""Future outbound-notification port. Reserved channels cannot send, even if called.

This is not a payment notify_url handler. Before implementing a channel, add a
transactional outbox, recipient/consent checks, template mapping, delivery receipts
and idempotent retries. Do not call it from GET endpoints or payment transactions.
"""
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class NotificationEnvelope:
    event_key: str  # queue + object ID + event revision; not a raw channel payload
    recipient_id: str  # internal ID; resolve contact information server-side only
    template_key: str
    target_page: str


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["not_configured", "accepted", "delivered", "failed"]
    provider_reference: str = ""


class NotificationChannel(Protocol):
    def send(self, envelope: NotificationEnvelope) -> DeliveryResult: ...


class ReservedChannel:
    def send(self, envelope: NotificationEnvelope) -> DeliveryResult:
        return DeliveryResult(status="not_configured")


def get_external_channel(name: str) -> NotificationChannel:
    if name not in ("sms", "wechat"):
        raise ValueError("Unsupported notification channel")
    return ReservedChannel()


def channel_capabilities():
    return [
        {"key": "admin_inbox", "label": "后台通知", "enabled": True, "status": "available"},
        {"key": "sms", "label": "短信", "enabled": False, "status": "reserved"},
        {"key": "wechat", "label": "微信", "enabled": False, "status": "reserved"},
    ]
