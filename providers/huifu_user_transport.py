"""Scoped strict-response guard around the pinned *official* SDK transport.

The SDK returns unsigned JSON verbatim and discards signed envelopes. Checking
only its return value cannot prove verification. The project therefore wraps
the parser while holding the SAME lock used by payments, requires a signed
envelope, and delegates signature verification to the original SDK parser.
The original methods/configuration are restored even on failure. No SDK files,
HTTP transport, TLS behavior or other payment business rules are replaced.
"""
import base64
import json
import re

from django.views.decorators.debug import sensitive_variables

from orders.huifu import _SDK_LOCK
from .huifu_user import ChannelUncertain, OnboardingUnavailable


SUPPORTED_SDK_VERSION = "2.0.24"
MAX_RESPONSE_BYTES = 1024 * 1024
_ABSENT = object()
_REQUEST_STATE = (
    "request_url", "product_id", "sys_id", "request_params", "version", "sdk_version",
    "private_key", "public_key", "connect_timeout", "need_sign", "need_verfy_sign",
    "jpt_x_skill_source", "jpt_x_skill_huifu_id",
)
_ROUTES = {
    "V2UserBasicdataIndvRequest": "/v2/user/basicdata/indv",
    "V2UserBusiOpenRequest": "/v2/user/busi/open",
    "V2UserBasicdataQueryRequest": "/v2/user/basicdata/query",
    "V2UserListQueryRequest": "/v2/user/list/query",
}


def ensure_transport_ready():
    from dg_sdk import DGClient
    from dg_sdk.core import log_util
    from dg_sdk.core.api_request import ApiRequest

    # An SDK upgrade must explicitly revalidate the internal parser contract.
    if DGClient.__version__ != SUPPORTED_SDK_VERSION or DGClient.BASE_URL != "https://api.huifu.com":
        raise OnboardingUnavailable("开户 SDK 版本或渠道地址未经适配验证，请联系平台。")
    if log_util.log_level:
        raise OnboardingUnavailable("平台开户 SDK 调试日志必须关闭，请联系平台。")
    for name in ("_build_return_data", "_build_request_info"):
        if not isinstance(ApiRequest.__dict__.get(name), staticmethod):
            raise OnboardingUnavailable("开户 SDK 接口不兼容，请联系平台。")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-JSON numeric constant")


def _signature_present(value):
    # Enforce envelope structure; cryptographic verification is still the SDK's.
    if not isinstance(value, str) or not value or len(value) > 1024:
        return False
    try:
        return len(base64.b64decode(value, validate=True)) >= 256
    except ValueError:
        return False


@sensitive_variables()
def sdk_call(request, extra, config):
    from dg_sdk import DGClient, MerConfig
    from dg_sdk.core.api_request import ApiRequest

    verified_result = None
    try:
        with _SDK_LOCK:
            ensure_transport_ready()
            route = _ROUTES.get(type(request).__name__)
            if not route or not type(request).__module__.startswith("dg_sdk.request."):
                raise ChannelUncertain()
            if any(hasattr(request, key) for key in extra):
                raise ChannelUncertain()  # Never allow extensions to overwrite explicit fields.
            expected_url = "https://api.huifu.com" + route
            client_before = {key: getattr(DGClient, key, _ABSENT) for key in ("env", "mer_config", "connect_timeout")}
            state_before = {key: getattr(ApiRequest, key) for key in _REQUEST_STATE}
            parser_before = ApiRequest.__dict__["_build_return_data"]
            builder_before = ApiRequest.__dict__["_build_request_info"]
            original_parser = ApiRequest._build_return_data
            original_builder = ApiRequest._build_request_info

            @sensitive_variables()
            def checked_request(url, params, files):
                if url != expected_url or files or not ApiRequest.need_sign or not ApiRequest.need_verfy_sign:
                    raise ChannelUncertain()
                headers, body = original_builder(url, params, files)
                if not _signature_present(body.get("sign")) or body.get("sys_id") != config.sys_id or body.get("product_id") != config.product_id:
                    raise ChannelUncertain()
                return headers, body

            @sensitive_variables()
            def checked_response(response):
                nonlocal verified_result
                if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
                    raise ChannelUncertain()
                envelope = json.loads(response.text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                if not isinstance(envelope, dict) or not _signature_present(envelope.get("sign")) or not isinstance(envelope.get("data"), dict):
                    raise ChannelUncertain()
                if not ApiRequest.need_verfy_sign or ApiRequest.public_key != config.public_key:
                    raise ChannelUncertain()
                # Original SDK parser performs SHA256WithRSA verification and raises
                # on a mismatch. No success/error business fields are trusted earlier.
                result = original_parser(response)
                if not isinstance(result, dict) or result != envelope["data"]:
                    raise ChannelUncertain()
                code = result.get("resp_code")
                if not isinstance(code, str) or not re.fullmatch(r"[0-9]{8}", code):
                    raise ChannelUncertain()
                for key in ("req_seq_id", "req_date"):
                    if key in result and result[key] != getattr(request, key):
                        raise ChannelUncertain()
                verified_result = result
                return result

            try:
                DGClient.env = "prod"
                DGClient.connect_timeout = 15
                DGClient.mer_config = MerConfig(
                    private_key=config.private_key, public_key=config.public_key,
                    sys_id=config.sys_id, product_id=config.product_id,
                    jpt_x_skill_source=config.skill_source,
                )
                ApiRequest._build_request_info = staticmethod(checked_request)
                ApiRequest._build_return_data = staticmethod(checked_response)
                result = request.post(extra)
                if verified_result is None or result is not verified_result:
                    raise ChannelUncertain()
                # Initial passwords and unused personal fields must not leave the adapter.
                used = {"resp_code", "huifu_id", "apply_no", "resp_business", "indv_base_info",
                        "card_info", "settle_config_list", "user_list_info_list"}
                return {key: value for key, value in result.items() if key in used}
            finally:
                ApiRequest._build_return_data = parser_before
                ApiRequest._build_request_info = builder_before
                for key, value in state_before.items():
                    setattr(ApiRequest, key, value)
                for key, value in client_before.items():
                    if value is _ABSENT:
                        delattr(DGClient, key)
                    else:
                        setattr(DGClient, key, value)
    except Exception:
        # SDK/HTTP exception strings can contain personal data or signed request
        # bodies. Surface only an opaque uncertain outcome; never retry creation.
        raise ChannelUncertain("Channel response could not be verified") from None
