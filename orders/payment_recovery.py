"""Recover an existing Huifu request; never turn an uncertain request into a new charge."""

import logging
from functools import wraps

from django.utils import timezone
from rest_framework.exceptions import APIException

from .huifu import HuifuGatewayError, HuifuPaymentSessionInProgress

logger = logging.getLogger(__name__)


class PaymentRecoveryRequired(Exception):
    """Internal signal: leave the business transaction before querying the gateway."""

    def __init__(self, payment, **context):
        self.payment = payment
        self.context = context


class PaymentRecoveryNotice(APIException):
    status_code = 409

    def __init__(self, message, code):
        super().__init__({"detail": message, "code": code}, code=code)


def record_payment_error(payment, error, *, operation="create"):
    # No payload, credentials, payer identity or full order number in application logs.
    logger.warning(
        "Huifu %s failed: model=%s id=%s attempt=%s code=%s description=%s digest=%s",
        operation, payment._meta.label_lower, payment.pk, payment.preorder_attempts,
        error.response_code, error.response_description, error.response_digest,
    )


def pending_notice():
    return PaymentRecoveryNotice(
        "原支付结果尚未确认，请稍后查询；请勿重复付款。如需重付，请先取消原订单并确认关单。",
        "huifu_payment_pending_confirmation",
    )


def recoverable_payment_session(*, query_payment, apply_success, replay, apply_failure=None):
    """One gateway create per persisted request, with query-only recovery afterwards.

    Query does not promise pay_info. A missing session must not be fabricated or
    recreated under another identity while the original transaction can still pay.
    """
    def decorate(create_session):
        @wraps(create_session)
        def wrapped(*args, **kwargs):
            try:
                return create_session(*args, **kwargs)
            except PaymentRecoveryRequired as recovery:
                payment, context = recovery.payment, recovery.context

            try:
                query = query_payment(payment)
            except HuifuGatewayError as error:
                record_payment_error(payment, error, operation="recovery-query")
                # Not found can be an in-flight/visibility race, not proof of no charge.
                raise pending_notice() from error

            if (query.req_date, query.req_seq_id, query.huifu_id) != (
                payment.req_date, payment.req_seq_id, payment.gateway_merchant_id,
            ):
                raise HuifuGatewayError("支付恢复查询的订单标识不一致，需要人工核对。")
            if query.trans_stat == "S":
                apply_success(payment, query)
                raise PaymentRecoveryNotice(
                    "原支付结果已确认，请查看最新订单状态。", "huifu_payment_status_updated",
                )
            if query.trans_stat == "F":
                if apply_failure:
                    apply_failure(payment, query)
                raise PaymentRecoveryNotice(
                    "原支付交易已失败，请取消原订单后重新下单；充值请重新创建充值单。",
                    "huifu_payment_closed",
                )
            if query.trans_stat != "P":
                raise pending_notice()

            identity = (payment.req_date, payment.req_seq_id)
            payment.refresh_from_db()
            if (payment.req_date, payment.req_seq_id) != identity:
                raise HuifuPaymentSessionInProgress()
            expires_at = context.get("expires_at", getattr(payment, "expires_at", None))
            if expires_at is None or expires_at <= timezone.now():
                raise pending_notice()
            if not payment.payment_invoke_payload:
                raise pending_notice()
            # An old concurrent error may have changed READY to FAILED. Do not erase
            # the cached session or issue create again; replay only after validation.
            return replay(payment, context), False
        return wrapped
    return decorate
