import json
import threading
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from urllib.parse import urlsplit

from django.conf import settings
from rest_framework.exceptions import APIException


HUIFU_ACCEPTED_CODES = {"00000000", "00000100"}
HUIFU_PAYMENT_STATUSES = {"", "I", "P", "S", "F"}
_SDK_LOCK = threading.RLock()


class HuifuConfigurationError(APIException):
    status_code = 503
    default_detail = "支付通道尚未完成配置，请稍后重试。"
    default_code = "huifu_not_configured"


class HuifuGatewayError(APIException):
    status_code = 502
    default_detail = "支付通道暂时不可用，请稍后重试。"
    default_code = "huifu_gateway_error"

    def __init__(self, detail=None, *, response_code="", response_digest=""):
        super().__init__(detail or self.default_detail, code=self.default_code)
        self.response_code = response_code
        self.response_digest = response_digest


class HuifuPaymentSessionInProgress(APIException):
    status_code = 409
    default_detail = "支付会话正在创建，请勿重复提交。"
    default_code = "huifu_payment_session_in_progress"


@dataclass(frozen=True)
class HuifuPaymentConfig:
    enabled: bool
    environment: str
    sys_id: str
    product_id: str
    merchant_id: str
    private_key: str
    public_key: str
    skill_source: str
    notify_url: str
    wechat_official_account_app_id: str
    wechat_mobile_app_id: str
    fee_flag: str
    connect_timeout_seconds: int

    @classmethod
    def from_settings(cls):
        return cls(
            enabled=settings.HUIFU_PAYMENT_ENABLED,
            environment=settings.HUIFU_ENV.strip(),
            sys_id=settings.HUIFU_SYS_ID.strip(),
            product_id=settings.HUIFU_PRODUCT_ID.strip(),
            merchant_id=settings.HUIFU_MERCHANT_ID.strip(),
            private_key=_normalise_pem(settings.HUIFU_RSA_PRIVATE_KEY),
            public_key=_normalise_pem(settings.HUIFU_RSA_PUBLIC_KEY),
            skill_source=settings.HUIFU_SKILL_SOURCE.strip(),
            notify_url=settings.HUIFU_NOTIFY_URL.strip(),
            wechat_official_account_app_id=settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip(),
            wechat_mobile_app_id=settings.WECHAT_MOBILE_APP_ID.strip(),
            fee_flag=settings.HUIFU_FEE_FLAG.strip(),
            connect_timeout_seconds=settings.HUIFU_CONNECT_TIMEOUT_SECONDS,
        )

    def validate_common(self):
        if not self.enabled:
            raise HuifuConfigurationError()
        required = {
            "HUIFU_SYS_ID": self.sys_id,
            "HUIFU_PRODUCT_ID": self.product_id,
            "HUIFU_MERCHANT_ID": self.merchant_id,
            "HUIFU_RSA_PRIVATE_KEY": self.private_key,
            "HUIFU_RSA_PUBLIC_KEY": self.public_key,
            "HUIFU_SKILL_SOURCE": self.skill_source,
            "HUIFU_NOTIFY_URL": self.notify_url,
        }
        if any(not value for value in required.values()):
            raise HuifuConfigurationError()
        if self.environment not in {"mertest", "prod"}:
            raise HuifuConfigurationError("支付通道环境配置无效。")
        if self.fee_flag not in {"1", "2"}:
            raise HuifuConfigurationError("支付手续费扣款配置无效。")
        if not 1 <= self.connect_timeout_seconds <= 60:
            raise HuifuConfigurationError("支付通道超时配置无效。")
        _validate_notify_url(self.notify_url)

    def validate_for_payment(self, *, trade_type: str):
        self.validate_common()
        if trade_type == "T_JSAPI":
            app_id = self.wechat_official_account_app_id
        elif trade_type == "T_APP":
            app_id = self.wechat_mobile_app_id
        else:
            raise HuifuConfigurationError("当前支付场景尚未开放。")
        if not app_id:
            raise HuifuConfigurationError()


@dataclass(frozen=True)
class HuifuPaymentSessionResult:
    req_seq_id: str
    req_date: str
    huifu_id: str
    trade_type: str
    trans_stat: str
    hf_seq_id: str
    party_order_id: str
    out_trans_id: str
    pay_info: dict
    response_code: str
    response_digest: str


@dataclass(frozen=True)
class HuifuPaymentQueryResult:
    req_date: str
    req_seq_id: str
    huifu_id: str
    trans_stat: str
    trans_amt: str
    end_time: str
    trade_type: str
    gateway_trade_no: str
    party_order_id: str
    out_trans_id: str
    response_code: str
    response_digest: str


def _normalise_pem(value: str) -> str:
    return (value or "").strip().replace("\\n", "\n")


def _validate_notify_url(value: str):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HuifuConfigurationError("支付异步通知地址必须是可公开访问的 HTTPS 地址。")
    if parsed.query or parsed.fragment:
        raise HuifuConfigurationError("支付异步通知地址不能包含查询参数或片段。")


def _canonical_digest(payload) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _response_data(response):
    if not isinstance(response, dict):
        return None
    nested = response.get("data")
    if "resp_code" not in response and isinstance(nested, dict):
        return nested
    return response


def _decode_pay_info(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        raise HuifuGatewayError("支付通道未返回有效的支付调起参数。")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise HuifuGatewayError("支付通道返回的支付调起参数格式无效。") from exc
    if not isinstance(decoded, dict):
        raise HuifuGatewayError("支付通道返回的支付调起参数格式无效。")
    return decoded


def cents_to_yuan(amount: int) -> str:
    return format(Decimal(amount) / Decimal("100"), ".2f")


class HuifuAggregatePaymentGateway:
    """Official dg-sdk adapter for V4 aggregate payment create and query calls."""

    def __init__(self, config: HuifuPaymentConfig):
        self.config = config

    @property
    def merchant_id(self) -> str:
        return self.config.merchant_id

    def validate_for_payment(self, *, trade_type: str):
        self.config.validate_for_payment(trade_type=trade_type)

    def _init_sdk(self):
        from dg_sdk import DGClient, MerConfig

        DGClient.env = self.config.environment
        DGClient.connect_timeout = self.config.connect_timeout_seconds
        DGClient.mer_config = MerConfig(
            self.config.private_key,
            self.config.public_key,
            self.config.sys_id,
            self.config.product_id,
            self.config.skill_source,
        )

    def create_payment(
        self,
        *,
        req_date: str,
        req_seq_id: str,
        amount: int,
        goods_desc: str,
        trade_type: str,
        attach: str,
        time_expire: str,
        sub_openid: str = "",
    ) -> HuifuPaymentSessionResult:
        from dg_sdk import Payment, PaymentCreateRequest

        self.config.validate_for_payment(trade_type=trade_type)
        method_expand = {"attach": attach[:128]}
        if trade_type == "T_JSAPI":
            if not sub_openid:
                raise HuifuConfigurationError("当前用户尚未完成微信服务号授权。")
            method_expand.update(
                {
                    "sub_appid": self.config.wechat_official_account_app_id,
                    "sub_openid": sub_openid,
                }
            )
        elif trade_type == "T_APP":
            method_expand["sub_appid"] = self.config.wechat_mobile_app_id

        try:
            # dg-sdk keeps request configuration on process-global classes.
            with _SDK_LOCK:
                self._init_sdk()
                request = PaymentCreateRequest()
                request.req_date = req_date
                request.req_seq_id = req_seq_id
                request.huifu_id = self.config.merchant_id
                request.trade_type = trade_type
                request.trans_amt = cents_to_yuan(amount)
                request.goods_desc = goods_desc[:128]
                request.time_expire = time_expire
                request.delay_acct_flag = "N"
                request.fee_flag = self.config.fee_flag
                request.notify_url = self.config.notify_url
                request.method_expand = json.dumps(
                    method_expand,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                response = Payment.create(request)
        except (HuifuConfigurationError, HuifuGatewayError):
            raise
        except Exception as exc:
            digest = sha256(type(exc).__name__.encode("utf-8")).hexdigest()
            raise HuifuGatewayError(response_digest=digest) from exc

        digest = _canonical_digest(response)
        data = _response_data(response)
        if data is None:
            raise HuifuGatewayError(response_digest=digest)
        response_code = str(data.get("resp_code", ""))
        if response_code not in HUIFU_ACCEPTED_CODES:
            raise HuifuGatewayError(response_code=response_code, response_digest=digest)
        expected = {
            "req_date": req_date,
            "req_seq_id": req_seq_id,
            "huifu_id": self.config.merchant_id,
        }
        if any(str(data.get(key, "")) != value for key, value in expected.items()):
            raise HuifuGatewayError(
                "支付通道返回的订单标识不一致。",
                response_code=response_code,
                response_digest=digest,
            )
        returned_trade_type = str(data.get("trade_type", ""))
        if returned_trade_type and returned_trade_type != trade_type:
            raise HuifuGatewayError(
                "支付通道返回的交易类型不一致。",
                response_code=response_code,
                response_digest=digest,
            )
        trans_stat = str(data.get("trans_stat", ""))
        if trans_stat not in HUIFU_PAYMENT_STATUSES or trans_stat == "F":
            raise HuifuGatewayError(response_code=response_code, response_digest=digest)

        return HuifuPaymentSessionResult(
            req_seq_id=req_seq_id,
            req_date=req_date,
            huifu_id=self.config.merchant_id,
            trade_type=trade_type,
            trans_stat=trans_stat,
            hf_seq_id=str(data.get("hf_seq_id", "")),
            party_order_id=str(data.get("party_order_id", "")),
            out_trans_id=str(data.get("out_trans_id", "")),
            pay_info=_decode_pay_info(data.get("pay_info")),
            response_code=response_code,
            response_digest=digest,
        )

    def query_payment(
        self,
        *,
        req_date: str,
        req_seq_id: str,
        hf_seq_id: str = "",
    ) -> HuifuPaymentQueryResult:
        from dg_sdk import Payment, PaymentQueryRequest

        self.config.validate_common()
        try:
            with _SDK_LOCK:
                self._init_sdk()
                request = PaymentQueryRequest()
                request.huifu_id = self.config.merchant_id
                request.req_date = req_date
                if hf_seq_id:
                    request.hf_seq_id = hf_seq_id
                else:
                    request.req_seq_id = req_seq_id
                response = Payment.query(request)
        except (HuifuConfigurationError, HuifuGatewayError):
            raise
        except Exception as exc:
            digest = sha256(type(exc).__name__.encode("utf-8")).hexdigest()
            raise HuifuGatewayError(response_digest=digest) from exc

        digest = _canonical_digest(response)
        data = _response_data(response)
        if data is None:
            raise HuifuGatewayError(response_digest=digest)
        response_code = str(data.get("resp_code", ""))
        if response_code not in HUIFU_ACCEPTED_CODES:
            raise HuifuGatewayError(response_code=response_code, response_digest=digest)
        if str(data.get("huifu_id", "")) != self.config.merchant_id:
            raise HuifuGatewayError(
                "支付查询返回的商户号不一致。",
                response_code=response_code,
                response_digest=digest,
            )
        returned_req_date = str(data.get("req_date", ""))
        returned_req_seq_id = str(data.get("req_seq_id", ""))
        if returned_req_date and returned_req_date != req_date:
            raise HuifuGatewayError(
                "支付查询返回的请求日期不一致。",
                response_code=response_code,
                response_digest=digest,
            )
        if returned_req_seq_id and returned_req_seq_id != req_seq_id:
            raise HuifuGatewayError(
                "支付查询返回的请求流水号不一致。",
                response_code=response_code,
                response_digest=digest,
            )
        returned_hf_seq_id = str(data.get("hf_seq_id", ""))
        if hf_seq_id and returned_hf_seq_id and returned_hf_seq_id != hf_seq_id:
            raise HuifuGatewayError(
                "支付查询返回的汇付全局流水号不一致。",
                response_code=response_code,
                response_digest=digest,
            )
        trans_stat = str(data.get("trans_stat", ""))
        if trans_stat not in HUIFU_PAYMENT_STATUSES or not trans_stat:
            raise HuifuGatewayError(
                "支付查询未返回有效交易状态。",
                response_code=response_code,
                response_digest=digest,
            )
        return HuifuPaymentQueryResult(
            req_date=returned_req_date or req_date,
            req_seq_id=returned_req_seq_id or req_seq_id,
            huifu_id=self.config.merchant_id,
            trans_stat=trans_stat,
            trans_amt=str(data.get("trans_amt", "")),
            end_time=str(data.get("end_time", "")),
            trade_type=str(data.get("trade_type", "")),
            gateway_trade_no=returned_hf_seq_id,
            party_order_id=str(data.get("party_order_id", "")),
            out_trans_id=str(data.get("out_trans_id", "")),
            response_code=response_code,
            response_digest=digest,
        )

    def verify_payment_notification(self, *, resp_data: str, sign: str) -> bool:
        from dg_sdk.core.rsa_utils import rsa_design

        try:
            verified, _ = rsa_design(sign, resp_data, self.config.public_key)
        except Exception:
            return False
        return bool(verified)


def get_huifu_payment_gateway() -> HuifuAggregatePaymentGateway:
    return HuifuAggregatePaymentGateway(HuifuPaymentConfig.from_settings())
