"""Tencent SMS 2021-01-11 adapter. No automatic retry or sensitive payload logging."""

from dataclasses import dataclass, field
import logging
import re

from django.conf import settings
from rest_framework.exceptions import APIException
from tencentcloud.common.credential import Credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.retry import NoopRetryer
from tencentcloud.sms.v20210111.models import SendSmsRequest
from tencentcloud.sms.v20210111.sms_client import SmsClient

# The SDK's DEBUG logger contains Authorization, phone numbers and OTP payloads.
logging.getLogger("tencentcloud_sdk_common").disabled = True
logger = logging.getLogger(__name__)

TEMPLATE_SETTINGS = {
    "register": "TENCENT_SMS_TEMPLATE_ID_REGISTER",
    "login": "TENCENT_SMS_TEMPLATE_ID_LOGIN",
    "reset_password": "TENCENT_SMS_TEMPLATE_ID_RESET_PASSWORD",
    "initial_password": "TENCENT_SMS_TEMPLATE_ID_INITIAL_PASSWORD",
    "wechat_bind": "TENCENT_SMS_TEMPLATE_ID_WECHAT_BIND",
    # Share wording only: old/new phone challenges remain independently verified.
    "change_phone_current": "TENCENT_SMS_TEMPLATE_ID_CHANGE_PHONE",
    "change_phone_new": "TENCENT_SMS_TEMPLATE_ID_CHANGE_PHONE",
}


class SmsDeliveryUnavailable(APIException):
    status_code = 503
    default_detail = "验证码暂时发送失败，请稍后重试。"
    default_code = "sms_delivery_unavailable"


def _safe_token(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else ""


def _unavailable(code, request_id=""):
    # Never interpolate exceptions, raw Message, response objects, phone or OTP.
    logger.warning("Tencent SMS rejected: code=%s request_id=%s",
                   _safe_token(code) or "InvalidResponse", _safe_token(request_id))
    return SmsDeliveryUnavailable()


@dataclass(frozen=True)
class SmsConfig:
    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    app_id: str
    sign_name: str
    template_id: str
    params: tuple[str, ...]
    region: str
    timeout: int

    @classmethod
    def load(cls, *, purpose=None):
        if purpose is not None and purpose not in TEMPLATE_SETTINGS:
            raise _unavailable("InvalidPurpose")
        override = getattr(settings, TEMPLATE_SETTINGS[purpose], "").strip() if purpose else ""
        # Fall back only when not configured, never after a rejected/uncertain send.
        template_id = override or settings.TENCENT_SMS_TEMPLATE_ID
        secret_id, secret_key = settings.TENCENT_SMS_SECRET_ID, settings.TENCENT_SMS_SECRET_KEY
        if not secret_id and not secret_key:
            secret_id = settings.TENCENT_CLOUD_SECRET_ID
            secret_key = settings.TENCENT_CLOUD_SECRET_KEY
        config = cls(
            secret_id=secret_id, secret_key=secret_key,
            app_id=settings.TENCENT_SMS_SDK_APP_ID,
            sign_name=settings.TENCENT_SMS_SIGN_NAME,
            template_id=template_id,
            params=tuple(settings.TENCENT_SMS_TEMPLATE_PARAMS),
            region=settings.TENCENT_SMS_REGION,
            timeout=settings.TENCENT_SMS_TIMEOUT_SECONDS,
        )
        if (
            not settings.TENCENT_SMS_ENABLED
            or not all((config.secret_id, config.secret_key, config.sign_name, config.region))
            or not re.fullmatch(r"[0-9]+", config.app_id)
            or not re.fullmatch(r"[0-9]+", config.template_id)
            or config.params not in (("code",), ("code", "minutes"))
            or settings.SMS_CODE_TTL_SECONDS <= 0
            or ("minutes" in config.params and settings.SMS_CODE_TTL_SECONDS % 60 != 0)
            or not 1 <= config.timeout <= 15
            or settings.SMS_PHONE_DAILY_LIMIT <= 0
            or settings.SMS_CODE_MAX_ATTEMPTS <= 0
        ):
            raise _unavailable("NotConfigured")
        return config


def send_verification_sms(*, phone: str, code: str, config: SmsConfig) -> None:
    """Success means provider acceptance, not a handset delivery receipt."""
    if not re.fullmatch(r"1[3-9][0-9]{9}", phone) or not re.fullmatch(r"[0-9]{6}", code):
        raise _unavailable("InvalidInput")
    request = SendSmsRequest()
    request.PhoneNumberSet = [f"+86{phone}"]
    request.SmsSdkAppId = config.app_id
    request.SignName = config.sign_name
    request.TemplateId = config.template_id
    values = {"code": code, "minutes": str(settings.SMS_CODE_TTL_SECONDS // 60)}
    request.TemplateParamSet = [values[name] for name in config.params]
    try:
        client = SmsClient(
            Credential(config.secret_id, config.secret_key), config.region,
            ClientProfile(
                httpProfile=HttpProfile(
                    endpoint="sms.tencentcloudapi.com", reqMethod="POST",
                    reqTimeout=config.timeout, protocol="https",
                ),
                retryer=NoopRetryer(),
            ),
        )
        response = client.SendSms(request)
    except TencentCloudSDKException as exc:
        raise _unavailable(exc.get_code(), exc.get_request_id()) from None
    except Exception:
        # Transport exceptions can contain the outgoing body or credentials.
        raise _unavailable("TransportError") from None

    statuses = getattr(response, "SendStatusSet", None)
    request_id = getattr(response, "RequestId", "")
    if not isinstance(statuses, list) or len(statuses) != 1:
        raise _unavailable("InvalidResponse", request_id)
    status = statuses[0]
    if getattr(status, "Code", "") != "Ok":
        raise _unavailable(getattr(status, "Code", ""), request_id)
    if (
        getattr(status, "PhoneNumber", "") != f"+86{phone}"
        or not _safe_token(getattr(status, "SerialNo", ""))
        or not _safe_token(request_id)
    ):
        raise _unavailable("InvalidResponse", request_id)
