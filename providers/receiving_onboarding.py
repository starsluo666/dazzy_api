"""Durable, consented onboarding. Uncertain requests are reconciled, never recreated."""
import hashlib
import hmac
import json
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.account_closure import lock_active_user_for_business
from .huifu_user import (
    ChannelUncertain, HuifuUserGateway, UserChannelConfig, business_payload,
    decode_field, digest, registration_payload,
)
from .models import ProviderProfile, ProviderReceivingAccount, ProviderReceivingAttempt, ProviderReceivingNotification
from .receiving_accounts import ReceivingDetailsSerializer, decrypt_details
from .receiving_regions import validate_bank_region


ONBOARDING_CONSENT_VERSION = "huifu-personal-cash-v2"
ONBOARDING_NOTICE = (
    "我确认以上为本人真实身份及本人储蓄卡信息，同意乐搭伴将姓名、身份证号及有效期、"
    "银行卡号、银行所在省市、银行预留手机号提交上海汇付支付有限公司，"
    "用于实名认证、个人收款用户开户、绑定本人提现卡及开通手动提现。"
    "订单满足结算条件且渠道分账核验成功后计入达人余额，由本人主动申请提现至银行卡，手续费由平台承担。"
    "开户结果以汇付核验为准；开通不代表订单已分账或银行卡已到账，不会在此扣款。"
    "提交后如需更正资料或注销渠道账户，请联系客服处理。"
)
LABELS = {
    "not_connected": "资料已保存，待渠道开通", "registering": "正在申请开户",
    "registered": "开户成功，待配置提现", "configuring": "正在配置手动提现",
    "pending": "渠道核验处理中", "active": "余额手动提现已开通",
    "rejected": "开户资料需修正", "attention": "渠道结果待核实",
}


def _attempt(account, kind, config=None):
    return ProviderReceivingAttempt.objects.create(
        account=account, kind=kind, req_seq_id=uuid.uuid4().hex,
        req_date=timezone.localdate().strftime("%Y%m%d"), settlement_config=config or {},
    )


def _scope(account, config):
    if account.channel_scope and account.channel_scope != config.scope:
        raise ValidationError("开户渠道配置已变更，请联系平台核实，不能跨渠道查询或重提。")


@sensitive_variables()
def _validate_materials(provider, details):
    if provider.status != ProviderProfile.Status.APPROVED or not provider.has_verified_identity:
        raise PermissionDenied("请先通过平台实名认证。")
    serializer = ReceivingDetailsSerializer(data=details)
    serializer.is_valid(raise_exception=True)
    expected = hmac.new(settings.SECRET_KEY.encode(), details["id_number"].encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provider.identity_number_digest) or details["real_name"] != provider.identity_real_name:
        raise ValidationError("收款资料与当前实名信息不一致，请重新核实。")
    if len(details["real_name"].encode("gb18030")) > 32:
        raise ValidationError("姓名超过渠道支持长度，请联系客服。")
    validate_bank_region(details.get("bank_province_code"), details.get("bank_city_code"))


@sensitive_variables()
def submit_onboarding(provider, data):
    config = UserChannelConfig.load(for_submission=True)
    if data.get("consent_accepted") is not True or data.get("consent_version") != ONBOARDING_CONSENT_VERSION:
        raise ValidationError("请阅读并确认当前开户及余额提现授权。")
    with transaction.atomic():
        lock_active_user_for_business(provider.user)
        provider = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
        account = ProviderReceivingAccount.objects.select_for_update().filter(provider=provider).first()
        if not account:
            raise ValidationError("请先保存本人收款资料。")
        _scope(account, config)
        if account.user_huifu_id and account.attempts.filter(kind="configure").exists() and account.onboarding_consent_version != ONBOARDING_CONSENT_VERSION:
            # Renew consent only. Never silently modify an existing live channel account.
            account.onboarding_consent_version = ONBOARDING_CONSENT_VERSION
            account.onboarding_consented_at = timezone.now()
            account.channel_status = "attention"
            account.cash_status = ""
            account.channel_message = "已确认手动提现授权；请平台在汇付侧关闭自动结算并配置取现，然后刷新核验。"
            account.save()
            consent = _attempt(account, "consent", config.settlement)
            consent.status = "succeeded"
            consent.finished_at = timezone.now()
            consent.save()
            return
        # Also prevents the browser double-tap/request retry starting a second creation.
        if account.channel_status not in {"not_connected", "rejected", "registered"}:
            return
        details = decrypt_details(account)
        _validate_materials(provider, details)
        if account.channel_status == "registered":
            kind = "configure"
        else:
            if account.user_huifu_id:
                raise ValidationError("已存在渠道账户，请先刷新状态。")
            kind = "register"
        account.channel_scope = config.scope
        account.onboarding_consent_version = ONBOARDING_CONSENT_VERSION
        account.onboarding_consented_at = timezone.now()
        account.channel_status = "registering" if kind == "register" else "configuring"
        account.channel_message = "请求已提交，请勿重复申请。"
        account.save()
        attempt = _attempt(account, kind, config.settlement)
        payload = registration_payload(attempt, details) if kind == "register" else business_payload(attempt, account, details, config)
    _execute(attempt, payload, config)
    if kind == "register":
        # A new transaction persists the Huifu user ID before the second external action.
        with transaction.atomic():
            lock_active_user_for_business(provider.user)
            account = ProviderReceivingAccount.objects.select_for_update().get(pk=account.pk)
            if account.channel_status != "registered":
                return
            account.channel_status = "configuring"
            account.save()
            opening = _attempt(account, "configure", attempt.settlement_config)
            payload = business_payload(opening, account, details, config)
        _execute(opening, payload, config)


@sensitive_variables()
def _execute(attempt, payload, config):
    try:
        response = HuifuUserGateway(config).call(attempt.kind, payload)
        if not isinstance(response, dict) or not isinstance(response.get("resp_code"), str) or not re.fullmatch(r"[0-9]{8}", response["resp_code"]):
            raise ChannelUncertain()
    except ChannelUncertain:
        _mark_uncertain(attempt)
        return
    with transaction.atomic():
        account = ProviderReceivingAccount.objects.select_for_update().get(pk=attempt.account_id)
        attempt = ProviderReceivingAttempt.objects.select_for_update().get(pk=attempt.pk)
        attempt.response_code = response.get("resp_code", "")
        attempt.response_digest = digest(response)
        attempt.finished_at = timezone.now()
        code = attempt.response_code
        if attempt.kind == "register":
            user_id = response.get("huifu_id", "")
            if code == "00000000" and isinstance(user_id, str) and re.fullmatch(r"[0-9]{1,18}", user_id):
                if user_id in {config.upper_id, config.sys_id} or ProviderReceivingAccount.objects.exclude(pk=account.pk).filter(user_huifu_id=user_id).exists():
                    attempt.status = "unknown"
                    account.channel_status = "attention"
                    account.channel_message = "渠道账户归属需核实，请联系客服。"
                else:
                    account.user_huifu_id = user_id
                    account.channel_status = "registered"
                    account.channel_message = "开户成功，正在准备本人银行卡与手动提现配置。"
                    attempt.status = "succeeded"
            elif code in {"00000001", "00000103", "00000104"}:
                # Only explicit pre-execution rejections. System/unknown codes never permit a new create.
                attempt.status = "rejected"
                account.channel_status = "rejected"
                account.channel_message = "渠道未受理开户，请核对身份资料或联系平台检查开户权限。"
            else:
                attempt.status = "unknown"
                account.channel_status = "attention"
                account.channel_message = "开户结果尚未确认，请刷新状态；不要重复开户。"
        else:
            if response.get("huifu_id") not in (None, "", account.user_huifu_id):
                attempt.status = "unknown"
                account.channel_status = "attention"
                account.channel_message = "渠道响应归属不一致，请联系客服。"
            else:
                apply_no = response.get("apply_no", "")
                if isinstance(apply_no, str) and len(apply_no) <= 18:
                    # Preserve an earlier, verified callback's application identity.
                    attempt.apply_no = attempt.apply_no or apply_no
                try:
                    statuses = _business_statuses(response)
                except ChannelUncertain:
                    statuses = {}
                # A callback can race with this response. Never roll back a reviewed terminal state.
                if account.audit_status not in {"Y", "N"}:
                    account.card_status = statuses.get("1", account.card_status)
                    account.settlement_status = statuses.get("3", account.settlement_status)
                    account.cash_status = statuses.get("2", account.cash_status)
                    account.channel_status = "pending" if code in {"00000000", "00000100", "90000000"} else "attention"
                    account.channel_message = "渠道已受理，请刷新状态确认银行卡与手动提现配置。" if account.channel_status == "pending" else "开户已保留，手动提现配置需核实，请联系客服。"
                if account.audit_status not in {"Y", "N"}:
                    attempt.status = "processing" if account.channel_status == "pending" else "unknown"
        attempt.save()
        account.save()


def _mark_uncertain(attempt):
    with transaction.atomic():
        account = ProviderReceivingAccount.objects.select_for_update().get(pk=attempt.account_id)
        attempt = ProviderReceivingAttempt.objects.select_for_update().get(pk=attempt.pk)
        if attempt.status == "sent":
            attempt.status = "unknown"
            attempt.finished_at = timezone.now()
            attempt.save()
            if account.channel_status != "active" and account.audit_status not in {"Y", "N"}:
                account.channel_status = "attention"
                account.channel_message = "渠道暂未返回可靠结果，请刷新状态；已提交请求不会重复发送。"
                account.save()


def _business_statuses(data):
    items = decode_field(data, "resp_business", list, optional=True)
    result = {}
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in {"1", "2", "3", "5"} or item.get("code") not in {"S", "F"} or item["type"] in result:
            raise ChannelUncertain("Invalid configuration result")
        result[item["type"]] = item["code"]
    return result


@sensitive_variables()
def refresh_onboarding(provider):
    config = UserChannelConfig.load()
    with transaction.atomic():
        lock_active_user_for_business(provider.user)
        account = ProviderReceivingAccount.objects.select_for_update().filter(provider=provider).first()
        if not account or not account.onboarding_consented_at:
            raise ValidationError("尚未提交开户申请。")
        _scope(account, config)
        if account.attempts.filter(kind__in=("register", "configure"), status="sent", created_at__gte=timezone.now() - timezone.timedelta(minutes=5)).exists():
            return
        if account.attempts.filter(kind__in=("query", "recover"), status="sent", created_at__gte=timezone.now() - timezone.timedelta(minutes=2)).exists():
            return
        details = decrypt_details(account)
        kind = "query" if account.user_huifu_id else "recover"
        attempt = _attempt(account, kind)
    try:
        gateway = HuifuUserGateway(config)
        base = {"req_seq_id": attempt.req_seq_id, "req_date": attempt.req_date}
        if kind == "recover":
            response = gateway.call("recover", {**base, "legal_cert_no": details["id_number"], "upper_huifu_id": config.upper_id})
            if response.get("resp_code") != "00000000":
                raise ChannelUncertain()
            # Table is String(JSONArray); isolate documented example conflict here only.
            items = response.get("user_list_info_list")
            if isinstance(items, str):
                items = json.loads(items)
            if not isinstance(items, list):
                raise ChannelUncertain()
            candidates = [item for item in items if isinstance(item, dict) and item.get("cust_type") == "2" and item.get("upper_huifu_id") == config.upper_id and item.get("name") == details["real_name"]]
            if len(candidates) != 1 or not re.fullmatch(r"[0-9]{1,18}", candidates[0].get("huifu_id", "")):
                raise ChannelUncertain()
            user_id = candidates[0]["huifu_id"]
            if user_id in {config.sys_id, config.upper_id}:
                raise ChannelUncertain()
            # Fresh query identity, bound to the same recovery attempt in persistent history.
            with transaction.atomic():
                ProviderReceivingAccount.objects.select_for_update().get(pk=account.pk)
                attempt.status = "succeeded"
                attempt.response_digest = digest(response)
                attempt.finished_at = timezone.now()
                attempt.save()
                attempt = _attempt(account, "query")
            base = {"req_seq_id": attempt.req_seq_id, "req_date": attempt.req_date}
        else:
            user_id = account.user_huifu_id
        response = gateway.call("query", {**base, "huifu_id": user_id})
        if response.get("resp_code") != "00000000":
            raise ChannelUncertain()
        if "huifu_id" in response and response["huifu_id"] != user_id:
            raise ChannelUncertain("Channel query belongs to a different user")
        identity = decode_field(response, "indv_base_info", dict)
        if identity.get("cert_type") != "00" or identity.get("cert_no") != details["id_number"] or identity.get("name") != details["real_name"]:
            raise ChannelUncertain()
        with transaction.atomic():
            account = ProviderReceivingAccount.objects.select_for_update().get(pk=account.pk)
            if ProviderReceivingAccount.objects.exclude(pk=account.pk).filter(user_huifu_id=user_id).exists():
                raise ChannelUncertain()
            account.user_huifu_id = user_id
            opening = account.attempts.filter(kind="configure").first()
            if not opening:
                account.channel_status = "registered"
                account.channel_message = "已确认本人渠道账户，请继续开通余额手动提现。"
            else:
                from .cash_accounts import verify_cash_configuration
                consent = account.attempts.filter(kind="consent").first()
                expected = (consent or opening).settlement_config
                ready = verify_cash_configuration(account, response, details, expected)
                if ready and account.onboarding_consent_version == ONBOARDING_CONSENT_VERSION and account.audit_status not in {"P", "N"}:
                    account.channel_status = "active"
                    account.channel_message = "已核验手动提现、本人银行卡及自动结算关闭；申请提现后以渠道到账结果为准。"
                else:
                    account.channel_status = "pending" if account.audit_status == "P" else "attention"
                    notices = []
                    if account.audit_status == "P":
                        notices.append("渠道审核中，暂不可提现。")
                    elif account.audit_status == "N":
                        notices.append("渠道审核未通过，请联系客服核实。")
                    if account.onboarding_consent_version != ONBOARDING_CONSENT_VERSION:
                        notices.append("需先确认当前手动提现授权。")
                    # The verifier records only fixed, non-sensitive descriptions.
                    notices.append(account.channel_message)
                    account.channel_message = "".join(notices)
            account.channel_checked_at = timezone.now()
            account.save()
            attempt.status = "succeeded"
            attempt.response_code = "00000000"
            attempt.response_digest = digest(response)
            attempt.finished_at = timezone.now()
            attempt.save()
    except (ChannelUncertain, ValueError, TypeError, KeyError):
        _mark_uncertain(attempt)


@sensitive_variables()
def receive_notification(raw, sign):
    from dg_sdk.core.rsa_utils import rsa_design
    from orders.huifu import _normalise_pem
    if not isinstance(raw, str) or not isinstance(sign, str) or not raw or len(raw) > 65536 or not sign or len(sign) > 1024:
        raise ValidationError("通知格式无效。")
    try:
        valid, _ = rsa_design(sign, raw, _normalise_pem(settings.HUIFU_RSA_PUBLIC_KEY))
    except Exception:
        valid = False
    if not valid:
        raise PermissionDenied("通知验签失败。")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("notify_type") != "A":
            raise ValueError
        if not isinstance(data.get("req_seq_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", data["req_seq_id"]):
            raise ValueError
        if not isinstance(data.get("req_date"), str) or not re.fullmatch(r"[0-9]{8}", data["req_date"]):
            raise ValueError
        if not isinstance(data.get("sub_resp_code"), str) or not re.fullmatch(r"[0-9]{8}", data["sub_resp_code"]):
            raise ValueError
        audit = decode_field(data, "audit_info", dict)
        state = audit.get("audit_status")
        if state not in {"Y", "P", "N"} or not re.fullmatch(r"[0-9A-Za-z]{1,18}", audit.get("apply_no", "")):
            raise ValueError
        statuses = _business_statuses(audit)
    except (ValueError, TypeError, ChannelUncertain):
        raise ValidationError("通知业务内容无效。") from None
    message_digest = hashlib.sha256(raw.encode()).hexdigest()
    config = UserChannelConfig.load()
    with transaction.atomic():
        reference = ProviderReceivingAttempt.objects.filter(kind="configure", req_seq_id=data.get("req_seq_id"), req_date=data.get("req_date")).first()
        if not reference:
            raise ValidationError("未找到通知关联申请。")
        account = ProviderReceivingAccount.objects.select_for_update().get(pk=reference.account_id)
        attempt = ProviderReceivingAttempt.objects.select_for_update().get(pk=reference.pk)
        _scope(account, config)
        if data.get("huifu_id") != account.user_huifu_id or (attempt.apply_no and attempt.apply_no != audit["apply_no"]):
            raise ValidationError("通知关联信息不匹配。")
        if not ProviderReceivingNotification.objects.filter(digest=message_digest).exists():
            ProviderReceivingNotification.objects.create(attempt=attempt, digest=message_digest, notify_type="A", audit_status=state)
            # Same request may progress P -> Y/N; stale P and conflicting terminal updates cannot undo it.
            if account.audit_status not in {"Y", "N"}:
                account.audit_status = state
                account.card_status = statuses.get("1", account.card_status)
                account.settlement_status = statuses.get("3", account.settlement_status)
                account.cash_status = statuses.get("2", account.cash_status)
                account.channel_status = "attention" if state == "N" or "F" in statuses.values() or data["sub_resp_code"] not in {"00000000", "00000100", "90000000"} else "pending"
                account.channel_message = "渠道审核未通过，请联系客服核实。" if state == "N" else "渠道状态已更新，请刷新核验本人银行卡与手动提现。"
                # Even Y + S is not a bank payout. Active is confirmed by detail query.
                account.save()
                attempt.status = "rejected" if state == "N" else "processing"
            attempt.apply_no = audit["apply_no"]
            attempt.save()
    return "RECV_ORD_ID_" + attempt.req_seq_id
