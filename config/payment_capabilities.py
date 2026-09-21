from django.conf import settings
from rest_framework.exceptions import ValidationError


def _huifu_scene_available(trade_type: str) -> bool:
    from orders.huifu import HuifuConfigurationError, HuifuPaymentConfig

    try:
        HuifuPaymentConfig.from_settings().validate_for_payment(trade_type=trade_type)
    except HuifuConfigurationError:
        return False
    return True


def payment_capabilities() -> dict[str, object]:
    mock_available = bool(settings.DEBUG)
    official_account_available = _huifu_scene_available("T_JSAPI")
    return {
        "provider_order": {
            "mock": {
                "available": mock_available,
                "reason": "" if mock_available else "模拟支付仅在本地开发环境开放。",
            },
            "official_account": {
                "available": official_account_available,
                "reason": (
                    ""
                    if official_account_available
                    else "微信服务号 H5 支付当前不可用，请稍后再试。"
                ),
            },
            # T_APP exists in the SDK, but the native client has no verified
            # invocation/result-confirmation flow yet, so it stays unavailable.
            "mobile_app": {
                "available": False,
                "reason": "当前版本暂未开放原生 App 支付，请使用微信服务号 H5。",
            },
        },
        "activity_publish": {
            "mock": {
                "available": mock_available,
                "reason": "" if mock_available else "活动发布真实支付尚未开放。",
            },
            "real": {
                "available": official_account_available,
                "reason": (
                    ""
                    if official_account_available
                    else "活动发布真实支付尚未开放。"
                ),
            },
        },
        "activity_participation": {
            "mock": {
                "available": mock_available,
                "reason": "" if mock_available else "活动报名真实支付尚未开放。",
            },
            "real": {
                "available": official_account_available,
                "reason": (
                    ""
                    if official_account_available
                    else "活动报名真实支付尚未开放。"
                ),
            },
        },
    }


def ensure_provider_order_payment_scene_available(payment_scene: str) -> None:
    scene = payment_capabilities()["provider_order"].get(payment_scene)
    if not scene or not scene["available"]:
        reason = scene["reason"] if scene else "当前支付场景尚未开放。"
        raise ValidationError({"payment_scene": reason})


def ensure_activity_payment_available(capability: str) -> None:
    item = payment_capabilities()[capability]
    if item["mock"]["available"] or item["real"]["available"]:
        return
    raise ValidationError({"payment": item["real"]["reason"]})


def ensure_activity_real_payment_available(capability: str) -> None:
    item = payment_capabilities()[capability]["real"]
    if item["available"]:
        return
    raise ValidationError({"payment": item["reason"]})
