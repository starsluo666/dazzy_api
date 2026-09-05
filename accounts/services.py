import secrets
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import ValidationError


SMS_PURPOSES = {"register", "login", "reset_password"}


def _code_key(phone: str, purpose: str) -> str:
    return f"auth:sms-code:{purpose}:{phone}"


def _cooldown_key(phone: str, purpose: str) -> str:
    return f"auth:sms-cooldown:{purpose}:{phone}"


def _failure_key(phone: str, purpose: str) -> str:
    return f"auth:failures:{purpose}:{phone}"


def _code_attempt_key(phone: str, purpose: str) -> str:
    return f"auth:sms-code-attempts:{purpose}:{phone}"


@dataclass(frozen=True)
class SmsCodeResult:
    expires_in: int
    retry_after: int
    debug_code: str | None = None


def send_sms_code(*, phone: str, purpose: str) -> SmsCodeResult:
    if purpose not in SMS_PURPOSES:
        raise ValidationError({"purpose": "不支持的验证码用途。"})
    if cache.get(_cooldown_key(phone, purpose)):
        raise ValidationError({"phone": "验证码发送过于频繁，请稍后再试。"})

    code = settings.SMS_DEVELOPMENT_CODE if settings.DEBUG else f"{secrets.randbelow(1_000_000):06d}"
    cache.set(_code_key(phone, purpose), code, settings.SMS_CODE_TTL_SECONDS)
    cache.delete(_code_attempt_key(phone, purpose))
    cache.set(_cooldown_key(phone, purpose), True, settings.SMS_CODE_RESEND_SECONDS)

    # TODO: 非 DEBUG 环境在此调用短信供应商；验证码绝不能写入日志或响应。
    return SmsCodeResult(
        expires_in=settings.SMS_CODE_TTL_SECONDS,
        retry_after=settings.SMS_CODE_RESEND_SECONDS,
        debug_code=code if settings.DEBUG else None,
    )


def verify_sms_code(*, phone: str, purpose: str, code: str) -> None:
    cached_code = cache.get(_code_key(phone, purpose))
    if not cached_code:
        raise ValidationError({"code": "验证码错误或已过期。"})
    if not secrets.compare_digest(str(cached_code), code):
        attempt_key = _code_attempt_key(phone, purpose)
        if cache.add(attempt_key, 1, settings.SMS_CODE_TTL_SECONDS):
            attempts = 1
        else:
            try:
                attempts = cache.incr(attempt_key)
            except ValueError:
                attempts = 1
                cache.set(attempt_key, attempts, settings.SMS_CODE_TTL_SECONDS)
        if attempts >= settings.SMS_CODE_MAX_ATTEMPTS:
            cache.delete(_code_key(phone, purpose))
            raise ValidationError({"code": "验证码尝试次数过多，请重新获取。"})
        raise ValidationError({"code": "验证码错误或已过期。"})
    cache.delete(_code_key(phone, purpose))
    cache.delete(_code_attempt_key(phone, purpose))


def ensure_auth_attempt_allowed(*, phone: str, purpose: str) -> None:
    if int(cache.get(_failure_key(phone, purpose), 0)) >= settings.AUTH_FAILURE_LIMIT:
        raise ValidationError("尝试次数过多，请15分钟后再试。")


def record_auth_failure(*, phone: str, purpose: str) -> None:
    key = _failure_key(phone, purpose)
    if cache.add(key, 1, settings.AUTH_LOCK_SECONDS):
        return
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, settings.AUTH_LOCK_SECONDS)


def clear_auth_failures(*, phone: str, purpose: str) -> None:
    cache.delete(_failure_key(phone, purpose))
