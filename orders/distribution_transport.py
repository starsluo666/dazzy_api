"""Verified envelopes around official SDK calls; no custom HTTP or signing.

This pilot shares the existing SDK global lock and envelope helpers with user
onboarding, but has a separate endpoint allowlist and business result parser.
"""

import json
import re

from Crypto.PublicKey import RSA
from django.views.decorators.debug import sensitive_variables

from providers.huifu_user_transport import (
    _ABSENT,
    _REQUEST_STATE,
    _reject_constant,
    _signature_present,
    _unique_object,
)
from .huifu import _SDK_LOCK, HuifuConfigurationError


class DistributionUncertain(Exception):
    """Never interpret an unverified/ambiguous result as success or safe to resend."""


def validate_transport(config):
    from dg_sdk import DGClient
    from dg_sdk.core import log_util
    from dg_sdk.core.api_request import ApiRequest
    from dg_sdk.core.rsa_utils import fill_private_key_marker, fill_public_key_marker

    config.validate_common()
    if (
        config.environment != "prod"
        or DGClient.BASE_URL != "https://api.huifu.com"
        or DGClient.__version__ != "2.0.24"
        or log_util.log_level
    ):
        raise HuifuConfigurationError("分账渠道环境、SDK 版本或日志配置未通过校验。")
    if any(
        not isinstance(ApiRequest.__dict__.get(key), staticmethod)
        for key in ("_build_return_data", "_build_request_info")
    ):
        raise HuifuConfigurationError("分账 SDK 需要重新适配。")
    try:
        private = RSA.import_key(fill_private_key_marker(config.private_key))
        public = RSA.import_key(fill_public_key_marker(config.public_key))
        if (
            not private.has_private()
            or public.has_private()
            or min(private.size_in_bits(), public.size_in_bits()) < 2048
        ):
            raise ValueError
    except (ValueError, TypeError, IndexError):
        raise HuifuConfigurationError("分账签名配置无效。") from None


@sensitive_variables()
def sdk_call(kind, payload, config):
    from dg_sdk import DGClient, MerConfig, Payment, PaymentQueryRequest
    from dg_sdk import (
        V2TradePaymentDelaytransConfirmRequest,
        V3TradePaymentDelaytransConfirmqueryRequest,
        V2TradeSettlementEncashmentRequest,
        V2TradeSettlementQueryRequest,
        V2TradeAcctpaymentBalanceQueryRequest,
    )
    from dg_sdk.core.api_request import ApiRequest

    routes = {
        "cash": ("/v2/trade/settlement/encashment", V2TradeSettlementEncashmentRequest),
        "cash_query": ("/v2/trade/settlement/query", V2TradeSettlementQueryRequest),
        "balance": ("/v2/trade/acctpayment/balance/query", V2TradeAcctpaymentBalanceQueryRequest),
        "payment_query": ("/v4/trade/payment/scanpay/query", PaymentQueryRequest),
        "confirm": ("/v2/trade/payment/delaytrans/confirm", V2TradePaymentDelaytransConfirmRequest),
        "confirm_query": (
            "/v3/trade/payment/delaytrans/confirmquery",
            V3TradePaymentDelaytransConfirmqueryRequest,
        ),
    }
    validate_transport(config)
    route, request_type = routes[kind]
    request, extra = request_type(), {}
    for key, value in payload.items():
        if hasattr(request, key):
            setattr(request, key, value)
        else:
            extra[key] = value
    if kind == "payment_query" and extra:
        raise DistributionUncertain()
    verified = None
    try:
        with _SDK_LOCK:
            # Payment and onboarding share the same process-global SDK state.
            validate_transport(config)
            client_before = {
                key: getattr(DGClient, key, _ABSENT)
                for key in ("env", "mer_config", "connect_timeout")
            }
            state_before = {key: getattr(ApiRequest, key) for key in _REQUEST_STATE}
            parser_before = ApiRequest.__dict__["_build_return_data"]
            builder_before = ApiRequest.__dict__["_build_request_info"]
            original_parser, original_builder = (
                ApiRequest._build_return_data,
                ApiRequest._build_request_info,
            )

            @sensitive_variables()
            def checked_request(url, params, files):
                if (
                    url != "https://api.huifu.com" + route
                    or files
                    or not ApiRequest.need_sign
                    or not ApiRequest.need_verfy_sign
                ):
                    raise DistributionUncertain()
                headers, body = original_builder(url, params, files)
                if (
                    not _signature_present(body.get("sign"))
                    or body.get("sys_id") != config.sys_id
                    or body.get("product_id") != config.product_id
                ):
                    raise DistributionUncertain()
                return headers, body

            @sensitive_variables()
            def checked_response(response):
                nonlocal verified
                if response.status_code != 200 or len(response.content) > 1024 * 1024:
                    raise DistributionUncertain()
                envelope = json.loads(
                    response.text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
                )
                if (
                    not isinstance(envelope, dict)
                    or not _signature_present(envelope.get("sign"))
                    or not isinstance(envelope.get("data"), dict)
                ):
                    raise DistributionUncertain()
                if not ApiRequest.need_verfy_sign or ApiRequest.public_key != config.public_key:
                    raise DistributionUncertain()
                result = original_parser(response)
                if (
                    not isinstance(result, dict)
                    or result != envelope["data"]
                    or not isinstance(result.get("resp_code"), str)
                    or not re.fullmatch(r"[0-9]{8}", result["resp_code"])
                ):
                    raise DistributionUncertain()
                verified = result
                return result

            try:
                DGClient.env = "prod"
                DGClient.connect_timeout = config.connect_timeout_seconds
                DGClient.mer_config = MerConfig(
                    private_key=config.private_key,
                    public_key=config.public_key,
                    sys_id=config.sys_id,
                    product_id=config.product_id,
                    jpt_x_skill_source=config.skill_source,
                )
                ApiRequest._build_request_info = staticmethod(checked_request)
                ApiRequest._build_return_data = staticmethod(checked_response)
                result = Payment.query(request) if kind == "payment_query" else request.post(extra)
                if verified is None or result is not verified:
                    raise DistributionUncertain()
                return result
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
        raise DistributionUncertain("分账渠道结果未能核实，请查询原流水，禁止重新出款。") from None
