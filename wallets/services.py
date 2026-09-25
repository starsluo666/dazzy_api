from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from orders.huifu import (
    HuifuGatewayError,
    HuifuPaymentSessionInProgress,
    get_huifu_payment_gateway,
)

from .models import (
    RechargeCampaign,
    WalletLedgerEntry,
    WalletPaymentAllocation,
    WalletRechargeNotification,
    WalletRechargeOrder,
    WalletRefundReceipt,
    UserWallet,
)


PAYMENT_SCENE_TRADE_TYPES = {
    "official_account": "T_JSAPI",
    "mobile_app": "T_APP",
}
RECHARGE_PAYMENT_TIMEOUT = timedelta(minutes=30)
PAYMENT_SESSION_STALE_AFTER = timedelta(seconds=30)


@dataclass(frozen=True)
class WalletPaymentBreakdown:
    payable_amount: int
    wallet_amount: int
    external_amount: int
    balance_sufficient: bool


def _wallet_for_update(user_id: int) -> UserWallet:
    wallet = UserWallet.objects.select_for_update().filter(user_id=user_id).first()
    if wallet:
        return wallet
    try:
        # Keep a concurrent first creation from breaking the caller's transaction.
        with transaction.atomic():
            wallet = UserWallet.objects.create(user_id=user_id)
    except IntegrityError:
        wallet = UserWallet.objects.select_for_update().get(user_id=user_id)
    return wallet


def get_or_create_wallet(user_id: int) -> UserWallet:
    wallet, _ = UserWallet.objects.get_or_create(user_id=user_id)
    return wallet


def _append_ledger(
    *,
    wallet: UserWallet,
    entry_type: str,
    available_delta: int,
    frozen_delta: int,
    idempotency_key: str,
    reference_type: str,
    reference_no: str,
    description: str,
    metadata: dict | None = None,
):
    return WalletLedgerEntry.objects.create(
        wallet=wallet,
        entry_type=entry_type,
        available_delta=available_delta,
        frozen_delta=frozen_delta,
        available_balance_after=wallet.available_balance,
        frozen_balance_after=wallet.frozen_balance,
        idempotency_key=idempotency_key,
        reference_type=reference_type,
        reference_no=reference_no,
        description=description,
        metadata=metadata or {},
    )


def preview_wallet_payment(*, user_id: int, payable_amount: int, business_type: str, business_order_no: str, external_only: bool = False):
    existing = WalletPaymentAllocation.objects.filter(
        business_type=business_type,
        business_order_no=business_order_no,
        user_id=user_id,
    ).first()
    if existing:
        return WalletPaymentBreakdown(
            payable_amount=existing.payable_amount,
            wallet_amount=existing.wallet_amount,
            external_amount=existing.external_amount,
            balance_sufficient=existing.external_amount == 0,
        )
    available = (
        UserWallet.objects.filter(user_id=user_id)
        .values_list("available_balance", flat=True)
        .first()
        or 0
    )
    # A pre-wallet gateway session must keep its original full-external amount,
    # including authorization endpoints which only ask for a preview.
    if available and not external_only:
        if business_type == WalletPaymentAllocation.BusinessType.PROVIDER_ORDER:
            from orders.models import ProviderOrderPaymentOrder

            external_only = ProviderOrderPaymentOrder.objects.filter(
                order__order_no=business_order_no,
            ).exclude(req_seq_id="").exists()
        else:
            from activities.models import ActivityHuifuPaymentOrder

            relation = "publish_order" if business_type == WalletPaymentAllocation.BusinessType.ACTIVITY_PUBLISH else "participation_order"
            external_only = ActivityHuifuPaymentOrder.objects.filter(
                **{f"{relation}__order_no": business_order_no}
            ).exclude(req_seq_id="").exists()
    wallet_amount = 0 if external_only else min(available, payable_amount)
    return WalletPaymentBreakdown(
        payable_amount=payable_amount,
        wallet_amount=wallet_amount,
        external_amount=payable_amount - wallet_amount,
        balance_sufficient=wallet_amount >= payable_amount,
    )


@transaction.atomic
def prepare_wallet_payment(
    *, user_id: int, business_type: str, business_order_no: str, payable_amount: int,
    external_only: bool = False,
) -> WalletPaymentAllocation:
    if payable_amount < 0:
        raise ValidationError({"payable_amount": "订单应付金额无效。"})
    existing = WalletPaymentAllocation.objects.select_for_update().filter(
        business_type=business_type,
        business_order_no=business_order_no,
    ).first()
    if existing:
        if existing.user_id != user_id or existing.payable_amount != payable_amount:
            raise ValidationError({"payment": "支付分配与当前订单不一致。"})
        if existing.wallet_released or existing.status == WalletPaymentAllocation.Status.RELEASED:
            raise ValidationError({"payment": "该支付分配已释放，不能重复发起。"})
        return existing

    wallet = _wallet_for_update(user_id)
    # A concurrent caller may have inserted the allocation while we waited on
    # the wallet. Business callers also hold their order lock.
    if WalletPaymentAllocation.objects.filter(
        business_type=business_type, business_order_no=business_order_no
    ).exists():
        return prepare_wallet_payment(
            user_id=user_id, business_type=business_type,
            business_order_no=business_order_no, payable_amount=payable_amount,
            external_only=external_only,
        )
    wallet_amount = 0 if external_only else min(wallet.available_balance, payable_amount)
    external_amount = payable_amount - wallet_amount
    allocation = WalletPaymentAllocation.objects.create(
        user_id=user_id,
        business_type=business_type,
        business_order_no=business_order_no,
        payable_amount=payable_amount,
        wallet_amount=wallet_amount,
        external_amount=external_amount,
    )
    if wallet_amount:
        wallet.available_balance -= wallet_amount
        wallet.frozen_balance += wallet_amount
        wallet.version += 1
        wallet.save(
            update_fields=("available_balance", "frozen_balance", "version", "updated_at")
        )
        _append_ledger(
            wallet=wallet,
            entry_type=WalletLedgerEntry.EntryType.PAYMENT_HOLD,
            available_delta=-wallet_amount,
            frozen_delta=wallet_amount,
            idempotency_key=f"wallet-payment-hold:{business_type}:{business_order_no}",
            reference_type=business_type,
            reference_no=business_order_no,
            description="订单支付冻结余额",
            metadata={"payable_amount": payable_amount, "external_amount": external_amount},
        )
    return allocation


@transaction.atomic
def consume_wallet_payment(*, business_type: str, business_order_no: str) -> WalletPaymentAllocation:
    allocation = WalletPaymentAllocation.objects.select_for_update().get(
        business_type=business_type,
        business_order_no=business_order_no,
    )
    if allocation.status in (
        WalletPaymentAllocation.Status.CONSUMED,
        WalletPaymentAllocation.Status.PARTIALLY_REFUNDED,
        WalletPaymentAllocation.Status.REFUNDED,
    ):
        return allocation
    if allocation.status != WalletPaymentAllocation.Status.HELD or allocation.wallet_released:
        raise ValidationError({"payment": "余额支付分配当前不可扣款。"})
    if allocation.wallet_amount:
        wallet = _wallet_for_update(allocation.user_id)
        if wallet.frozen_balance < allocation.wallet_amount:
            raise ValidationError({"payment": "钱包冻结余额不足，需要人工核对。"})
        wallet.frozen_balance -= allocation.wallet_amount
        wallet.version += 1
        wallet.save(update_fields=("frozen_balance", "version", "updated_at"))
        _append_ledger(
            wallet=wallet,
            entry_type=WalletLedgerEntry.EntryType.PAYMENT_CONSUME,
            available_delta=0,
            frozen_delta=-allocation.wallet_amount,
            idempotency_key=f"wallet-payment-consume:{business_type}:{business_order_no}",
            reference_type=business_type,
            reference_no=business_order_no,
            description="订单余额支付",
            metadata={"external_amount": allocation.external_amount},
        )
    allocation.status = WalletPaymentAllocation.Status.CONSUMED
    allocation.save(update_fields=("status", "updated_at"))
    return allocation


@transaction.atomic
def release_wallet_payment(*, business_type: str, business_order_no: str) -> WalletPaymentAllocation | None:
    allocation = WalletPaymentAllocation.objects.select_for_update().filter(
        business_type=business_type,
        business_order_no=business_order_no,
    ).first()
    if allocation is None or allocation.status == WalletPaymentAllocation.Status.RELEASED:
        return allocation
    if allocation.status != WalletPaymentAllocation.Status.HELD:
        return allocation
    if allocation.wallet_amount:
        wallet = _wallet_for_update(allocation.user_id)
        if wallet.frozen_balance < allocation.wallet_amount:
            raise ValidationError({"payment": "钱包冻结余额不足，需要人工核对。"})
        wallet.frozen_balance -= allocation.wallet_amount
        wallet.available_balance += allocation.wallet_amount
        wallet.version += 1
        wallet.save(
            update_fields=("available_balance", "frozen_balance", "version", "updated_at")
        )
        _append_ledger(
            wallet=wallet,
            entry_type=WalletLedgerEntry.EntryType.PAYMENT_RELEASE,
            available_delta=allocation.wallet_amount,
            frozen_delta=-allocation.wallet_amount,
            idempotency_key=f"wallet-payment-release:{business_type}:{business_order_no}",
            reference_type=business_type,
            reference_no=business_order_no,
            description="订单关闭，解冻余额",
        )
    allocation.status = WalletPaymentAllocation.Status.RELEASED
    allocation.wallet_released = True
    allocation.save(update_fields=("status", "wallet_released", "updated_at"))
    return allocation


@transaction.atomic
def complete_wallet_refund(
    *,
    business_type: str,
    business_order_no: str,
    wallet_refund_amount: int,
    external_refund_amount: int,
    refund_reference_no: str,
) -> WalletPaymentAllocation:
    allocation = WalletPaymentAllocation.objects.select_for_update().get(
        business_type=business_type,
        business_order_no=business_order_no,
    )
    existing = WalletRefundReceipt.objects.filter(
        reference_no=refund_reference_no
    ).first()
    if existing:
        if (
            existing.allocation_id != allocation.pk
            or existing.wallet_amount != wallet_refund_amount
            or existing.external_amount != external_refund_amount
        ):
            raise ValidationError({"refund": "退款幂等键对应的金额或订单不一致。"})
        return allocation
    if allocation.status == WalletPaymentAllocation.Status.HELD:
        raise ValidationError({"refund": "原支付尚未完成，不能退款。"})
    if (
        wallet_refund_amount < 0 or external_refund_amount < 0
        or allocation.wallet_refunded_amount + wallet_refund_amount > allocation.wallet_amount
        or allocation.external_refunded_amount + external_refund_amount > allocation.external_amount
    ):
        raise ValidationError({"refund": "退款金额超出原支付构成。"})
    was_released = allocation.wallet_released or allocation.status == WalletPaymentAllocation.Status.RELEASED
    if was_released and wallet_refund_amount:
        raise ValidationError({"refund": "已解冻的余额不能重复退款。"})
    wallet = _wallet_for_update(allocation.user_id)
    if wallet_refund_amount:
        wallet.available_balance += wallet_refund_amount
        wallet.version += 1
        wallet.save(update_fields=("available_balance", "version", "updated_at"))
        _append_ledger(
            wallet=wallet,
            entry_type=WalletLedgerEntry.EntryType.REFUND,
            available_delta=wallet_refund_amount,
            frozen_delta=0,
            idempotency_key=f"wallet-refund:{refund_reference_no}",
            reference_type=business_type,
            reference_no=business_order_no,
            description="订单退款退回余额",
            metadata={
                "refund_reference_no": refund_reference_no,
                "external_refund_amount": external_refund_amount,
            },
        )
    WalletRefundReceipt.objects.create(
        reference_no=refund_reference_no, allocation=allocation,
        wallet_amount=wallet_refund_amount, external_amount=external_refund_amount,
    )
    allocation.wallet_refunded_amount += wallet_refund_amount
    allocation.external_refunded_amount += external_refund_amount
    if was_released and allocation.external_refunded_amount >= allocation.external_amount:
        allocation.status = WalletPaymentAllocation.Status.REFUNDED
    elif (
        allocation.wallet_refunded_amount >= allocation.wallet_amount
        and allocation.external_refunded_amount >= allocation.external_amount
    ):
        allocation.status = WalletPaymentAllocation.Status.REFUNDED
    else:
        allocation.status = WalletPaymentAllocation.Status.PARTIALLY_REFUNDED
    allocation.save(
        update_fields=(
            "wallet_refunded_amount",
            "external_refunded_amount",
            "status",
            "updated_at",
        )
    )
    return allocation


def recharge_pricing(*, campaign: RechargeCampaign, quantity: int) -> dict:
    if quantity < 1 or quantity > campaign.max_quantity_per_order:
        raise ValidationError(
            {"quantity": f"单次可购买 1 至 {campaign.max_quantity_per_order} 张。"}
        )
    tier = campaign.discount_tiers.filter(min_quantity__lte=quantity).order_by(
        "-min_quantity"
    ).first()
    discount_rate_bps = tier.discount_rate_bps if tier else 10000
    credited_amount = campaign.unit_face_amount * quantity
    payable_amount = (credited_amount * discount_rate_bps + 5000) // 10000
    return {
        "unit_face_amount": campaign.unit_face_amount,
        "quantity": quantity,
        "credited_amount": credited_amount,
        "discount_rate_bps": discount_rate_bps,
        "discount_amount": credited_amount - payable_amount,
        "payable_amount": payable_amount,
    }


@transaction.atomic
def create_recharge_order(*, user_id: int, quantity: int) -> WalletRechargeOrder:
    from accounts.account_closure import lock_active_user_for_business
    from accounts.models import User

    lock_active_user_for_business(User(pk=user_id))
    campaign = RechargeCampaign.objects.select_for_update().filter(singleton_key=1).first()
    if campaign is None or not campaign.is_enabled:
        raise ValidationError({"recharge": "充值活动暂未开放。"})
    pricing = recharge_pricing(campaign=campaign, quantity=quantity)
    now = timezone.now()
    return WalletRechargeOrder.objects.create(
        user_id=user_id,
        **pricing,
        pricing_snapshot={
            **pricing,
            "campaign_updated_at": campaign.updated_at.isoformat(),
            "rounding": "half_up_cent",
        },
        expires_at=now + RECHARGE_PAYMENT_TIMEOUT,
    )


def recharge_order_payload(order: WalletRechargeOrder) -> dict:
    return {
        "order_no": order.order_no,
        "unit_face_amount": order.unit_face_amount,
        "quantity": order.quantity,
        "credited_amount": order.credited_amount,
        "discount_rate_bps": order.discount_rate_bps,
        "discount_amount": order.discount_amount,
        "payable_amount": order.payable_amount,
        "status": order.status,
        "status_label": order.get_status_display(),
        "expires_at": order.expires_at,
        "paid_at": order.paid_at,
        "closed_at": order.closed_at,
        "created_at": order.created_at,
    }


def create_recharge_huifu_payment_session(
    *, order_no: str, user_id: int, payment_scene: str, sub_openid: str = ""
):
    try:
        trade_type = PAYMENT_SCENE_TRADE_TYPES[payment_scene]
    except KeyError as exc:
        raise ValidationError({"payment_scene": "当前支付场景尚未开放。"}) from exc
    gateway = get_huifu_payment_gateway()
    gateway.validate_for_payment(trade_type=trade_type)
    if trade_type == "T_JSAPI" and not sub_openid:
        raise ValidationError({"authorization": "请先完成微信服务号网页授权。"})
    now = timezone.now()
    with transaction.atomic():
        order = WalletRechargeOrder.objects.select_for_update().filter(
            order_no=order_no, user_id=user_id
        ).first()
        if order is None:
            raise ValidationError({"order": "充值单不存在或无权操作。"})
        if order.status != WalletRechargeOrder.Status.PENDING_PAYMENT:
            raise ValidationError({"status": "充值单不在待支付状态。"})
        if order.expires_at <= now:
            # An expired local deadline does not prove the gateway is closed.
            # Keep it reconcilable in case a successful payment arrives late.
            raise ValidationError({"status": "充值单已过期，请重新创建。"})
        if (
            order.preorder_status == WalletRechargeOrder.PreorderStatus.READY
            and order.payment_scene == payment_scene
            and order.payment_invoke_payload
        ):
            return order, False
        if (
            order.preorder_status == WalletRechargeOrder.PreorderStatus.SUBMITTING
            and order.preorder_requested_at
            and order.preorder_requested_at > now - PAYMENT_SESSION_STALE_AFTER
        ):
            raise HuifuPaymentSessionInProgress()
        if order.payment_scene and order.payment_scene != payment_scene:
            raise ValidationError({"payment_scene": "当前充值单已绑定其他支付场景。"})
        order.req_date = order.req_date or timezone.localtime(now).strftime("%Y%m%d")
        order.req_seq_id = order.req_seq_id or order.order_no
        order.gateway_merchant_id = gateway.merchant_id
        order.payment_scene = payment_scene
        order.trade_type = trade_type
        order.preorder_status = WalletRechargeOrder.PreorderStatus.SUBMITTING
        order.preorder_requested_at = now
        order.preorder_attempts += 1
        order.payment_invoke_payload = {}
        order.save()
        req_date = order.req_date
        req_seq_id = order.req_seq_id
        amount = order.payable_amount
        time_expire = timezone.localtime(order.expires_at).strftime("%Y%m%d%H%M%S")
    try:
        result = gateway.create_payment(
            req_date=req_date,
            req_seq_id=req_seq_id,
            amount=amount,
            goods_desc=f"平台余额充值{order.quantity}张",
            trade_type=trade_type,
            attach=order.order_no,
            time_expire=time_expire,
            sub_openid=sub_openid,
        )
    except HuifuGatewayError as exc:
        WalletRechargeOrder.objects.filter(
            order_no=order_no, req_date=req_date, req_seq_id=req_seq_id
        ).update(
            preorder_status=WalletRechargeOrder.PreorderStatus.FAILED,
            gateway_response_code=exc.response_code[:32],
            gateway_response_digest=exc.response_digest[:64],
            updated_at=timezone.now(),
        )
        raise
    with transaction.atomic():
        order = WalletRechargeOrder.objects.select_for_update().get(order_no=order_no)
        if order.req_date != req_date or order.req_seq_id != req_seq_id:
            raise HuifuGatewayError("充值支付请求流水已变更，请重新进入充值页。")
        order.preorder_status = WalletRechargeOrder.PreorderStatus.READY
        order.gateway_merchant_id = result.huifu_id
        order.gateway_trade_no = result.hf_seq_id
        order.gateway_party_order_id = result.party_order_id
        order.gateway_out_trans_id = result.out_trans_id
        order.payment_invoke_payload = result.pay_info
        order.gateway_response_code = result.response_code
        order.gateway_response_digest = result.response_digest
        order.preorder_ready_at = timezone.now()
        order.save()
        return order, True


def _amount_to_cents(value: str) -> int:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HuifuGatewayError("支付通道返回的金额格式无效。") from exc
    cents = amount * Decimal("100")
    if cents != cents.to_integral_value() or cents < 0:
        raise HuifuGatewayError("支付通道返回的金额格式无效。")
    return int(cents)


def _paid_at(value: str):
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
    except (TypeError, ValueError) as exc:
        raise HuifuGatewayError("支付通道未返回有效的交易完成时间。") from exc
    return timezone.make_aware(parsed, timezone.get_current_timezone())


@transaction.atomic
def _credit_recharge_order(*, order_no: str, gateway_trade_no: str, paid_at):
    order = WalletRechargeOrder.objects.select_for_update().get(order_no=order_no)
    if order.status == WalletRechargeOrder.Status.PAID:
        return order, False
    if order.status != WalletRechargeOrder.Status.PENDING_PAYMENT:
        raise ValidationError({"status": "充值单已关闭，需要人工核对该笔到账。"})
    wallet = _wallet_for_update(order.user_id)
    wallet.available_balance += order.credited_amount
    wallet.version += 1
    wallet.save(update_fields=("available_balance", "version", "updated_at"))
    _append_ledger(
        wallet=wallet,
        entry_type=WalletLedgerEntry.EntryType.RECHARGE,
        available_delta=order.credited_amount,
        frozen_delta=0,
        idempotency_key=f"wallet-recharge:{order.order_no}",
        reference_type="wallet_recharge",
        reference_no=order.order_no,
        description=f"充值 {order.quantity} 张，面值到账",
        metadata={
            "payable_amount": order.payable_amount,
            "discount_amount": order.discount_amount,
        },
    )
    order.status = WalletRechargeOrder.Status.PAID
    order.gateway_trade_no = gateway_trade_no or order.gateway_trade_no
    order.paid_at = paid_at
    order.save(update_fields=("status", "gateway_trade_no", "paid_at", "updated_at"))
    return order, True


def confirm_recharge_payment(*, order_no: str, user_id: int | None = None):
    queryset = WalletRechargeOrder.objects.all()
    if user_id is not None:
        queryset = queryset.filter(user_id=user_id)
    order = queryset.filter(order_no=order_no).first()
    if order is None:
        raise ValidationError({"order": "充值单不存在或无权操作。"})
    if order.status == WalletRechargeOrder.Status.PAID:
        return order, False
    if not order.req_date or not order.req_seq_id or not order.gateway_merchant_id:
        changed = False
        if order.expires_at <= timezone.now():
            # Match the unsubmitted state in SQL as a session can start meanwhile.
            changed = bool(WalletRechargeOrder.objects.filter(
                pk=order.pk, status=WalletRechargeOrder.Status.PENDING_PAYMENT,
                req_seq_id="",
            ).update(status=WalletRechargeOrder.Status.CLOSED, closed_at=timezone.now()))
        order.refresh_from_db()
        return order, changed
    query = get_huifu_payment_gateway().query_payment(
        req_date=order.req_date,
        req_seq_id=order.req_seq_id,
        hf_seq_id=order.gateway_trade_no,
    )
    if query.huifu_id != order.gateway_merchant_id:
        raise HuifuGatewayError("充值查单返回的商户号与本地充值单不一致。")
    if (query.trans_amt and _amount_to_cents(query.trans_amt) != order.payable_amount) or (
        query.trans_stat == "S" and not query.trans_amt
    ):
        raise HuifuGatewayError("充值查单金额与本地充值单不一致。")
    if query.req_date != order.req_date or query.req_seq_id != order.req_seq_id:
        raise HuifuGatewayError("充值查单返回的流水与本地充值单不一致。")
    if query.trade_type and query.trade_type != order.trade_type:
        raise HuifuGatewayError("充值查单返回的交易类型与本地充值单不一致。")
    WalletRechargeOrder.objects.filter(pk=order.pk).update(
        gateway_last_query_status=query.trans_stat,
        gateway_last_query_digest=query.response_digest,
        gateway_last_queried_at=timezone.now(),
        updated_at=timezone.now(),
    )
    if query.trans_stat != "S":
        if query.trans_stat == "F":
            WalletRechargeOrder.objects.filter(
                pk=order.pk, status=WalletRechargeOrder.Status.PENDING_PAYMENT
            ).update(status=WalletRechargeOrder.Status.CLOSED, closed_at=timezone.now())
        return WalletRechargeOrder.objects.get(pk=order.pk), False
    return _credit_recharge_order(
        order_no=order.order_no,
        gateway_trade_no=query.gateway_trade_no or order.gateway_trade_no,
        paid_at=_paid_at(query.end_time),
    )


def process_recharge_huifu_payment_notification(*, fields: dict, payload_digest: str) -> str:
    order = WalletRechargeOrder.objects.filter(
        req_date=fields["req_date"], req_seq_id=fields["req_seq_id"]
    ).first()
    if order is None:
        raise ValidationError({"notification": "支付通知未匹配到本地支付单。"})
    if order.gateway_merchant_id != fields["huifu_id"]:
        raise ValidationError({"notification": "支付通知商户号与本地充值单不一致。"})
    if fields.get("trans_amt") and _amount_to_cents(fields["trans_amt"]) != order.payable_amount:
        raise ValidationError({"notification": "支付通知金额与本地充值单不一致。"})
    event_key = sha256(
        ":".join(
            (
                "wallet_recharge",
                fields["huifu_id"],
                fields["req_date"],
                fields["req_seq_id"],
                fields["hf_seq_id"],
                fields["trans_stat"],
            )
        ).encode("utf-8")
    ).hexdigest()
    with transaction.atomic():
        event, _ = WalletRechargeNotification.objects.get_or_create(
            event_key=event_key,
            defaults={
                "recharge_order": order,
                "payload_digest": payload_digest,
                "trans_stat": fields["trans_stat"],
                "hf_seq_id": fields["hf_seq_id"],
                "signature_verified": True,
            },
        )
        event = WalletRechargeNotification.objects.select_for_update().get(pk=event.pk)
        if event.payload_digest != payload_digest or event.recharge_order_id != order.pk:
            raise ValidationError({"notification": "充值通知幂等键冲突。"})
        if event.status == WalletRechargeNotification.Status.PROCESSED:
            return f"RECV_ORD_ID_{fields['req_seq_id']}"
    confirmed, _ = confirm_recharge_payment(order_no=order.order_no)
    if fields["trans_stat"] == "S" and confirmed.status != WalletRechargeOrder.Status.PAID:
        raise HuifuGatewayError("充值支付通知成功，但主动查单尚未确认成功。")
    with transaction.atomic():
        event = WalletRechargeNotification.objects.select_for_update().get(pk=event.pk)
        event.status = WalletRechargeNotification.Status.PROCESSED
        event.processed_at = timezone.now()
        event.query_response_digest = confirmed.gateway_last_query_digest
        event.save(
            update_fields=("status", "processed_at", "query_response_digest")
        )
    return f"RECV_ORD_ID_{fields['req_seq_id']}"
