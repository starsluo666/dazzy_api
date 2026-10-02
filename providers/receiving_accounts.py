"""Collect private receiving-account materials, without any channel or fund mutation.

Huifu onboarding needs a confirmed product contract. No saved record here may be
used as proof of channel readiness, a bound bank card, or successful settlement.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import date

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework import serializers
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from accounts.account_closure import lock_active_user_for_business
from .models import ProviderProfile, ProviderReceivingAccount


CONSENT_VERSION = "receiving-materials-v1"
COLLECTION_NOTICE = (
    "这些资料用于准备个人收款账户申请。身份证号、银行卡号和联系电话加密保存，"
    "仅向你展示脱敏号码。当前尚未向汇付提交开户或绑卡，不会发起分账或扣款。"
    "资料可以清除；正式提交渠道前会另行告知并征得你的同意。"
)
CHANNEL_NOTICE = "收款渠道尚未开通；保存资料不代表开户、绑卡或分账成功。"


class ReceivingAccountUnavailable(APIException):
    status_code = 503
    default_detail = "收款资料管理暂未开放，请稍后再试。"
    default_code = "receiving_account_unavailable"


@sensitive_variables()
def encryption_key():
    try:
        key = base64.b64decode(settings.PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY, validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise ReceivingAccountUnavailable() from None
    if len(key) != 32:
        raise ReceivingAccountUnavailable()
    return key


def collection_available():
    if not settings.PROVIDER_RECEIVING_ACCOUNT_COLLECTION_ENABLED:
        return False
    try:
        encryption_key()
    except ReceivingAccountUnavailable:
        return False
    return True


def _aad(provider_id):
    # Bind ciphertext to its owner so copying a DB cell cannot swap recipients.
    return f"provider-receiving-account:v1:{provider_id}".encode()


@sensitive_variables()
def encrypt_details(provider_id, details):
    cipher = AES.new(encryption_key(), AES.MODE_GCM, nonce=get_random_bytes(12))
    cipher.update(_aad(provider_id))
    ciphertext, tag = cipher.encrypt_and_digest(
        json.dumps(details, ensure_ascii=False, separators=(",", ":")).encode()
    )
    return "v1:" + base64.b64encode(cipher.nonce + tag + ciphertext).decode()


@sensitive_variables()
def decrypt_details(account):
    try:
        version, encoded = account.details_ciphertext.split(":", 1)
        if version != "v1":
            raise ValueError
        blob = base64.b64decode(encoded, validate=True)
        cipher = AES.new(encryption_key(), AES.MODE_GCM, nonce=blob[:12])
        cipher.update(_aad(account.provider_id))
        details = json.loads(cipher.decrypt_and_verify(blob[28:], blob[12:28]))
        if not isinstance(details, dict):
            raise ValueError
        return details
    except (ValueError, TypeError, KeyError, binascii.Error):
        raise ReceivingAccountUnavailable("收款资料暂时无法读取，请联系客服处理。") from None


class ReceivingDetailsSerializer(serializers.Serializer):
    id_number = serializers.RegexField(r"^[0-9]{17}[0-9X]$", max_length=18)
    cert_begin_date = serializers.DateField()
    cert_long_term = serializers.BooleanField()
    cert_end_date = serializers.DateField(required=False, allow_null=True)
    mobile = serializers.RegexField(r"^1[3-9][0-9]{9}$", max_length=11)
    bank_card_number = serializers.RegexField(r"^[0-9]{12,19}$", max_length=19)
    bank_name = serializers.CharField(max_length=60)
    bank_province = serializers.CharField(max_length=40)
    bank_city = serializers.CharField(max_length=40)

    def validate_id_number(self, value):
        # Catch obvious input errors before collecting a mismatched identity.
        weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
        checksum = "10X98765432"[sum(int(n) * w for n, w in zip(value[:17], weights)) % 11]
        try:
            birthday = date(int(value[6:10]), int(value[10:12]), int(value[12:14]))
        except ValueError:
            raise serializers.ValidationError("请输入有效的身份证号码。") from None
        if checksum != value[-1] or birthday >= timezone.localdate():
            raise serializers.ValidationError("请输入有效的身份证号码。")
        return value

    def validate(self, attrs):
        today = timezone.localdate()
        if attrs["cert_begin_date"] > today:
            raise serializers.ValidationError({"cert_begin_date": "证件生效日期不能晚于今天。"})
        if attrs["cert_long_term"]:
            attrs["cert_end_date"] = None
        else:
            end = attrs.get("cert_end_date")
            if not end or end < today or end <= attrs["cert_begin_date"]:
                raise serializers.ValidationError({"cert_end_date": "请填写未过期且晚于生效日期的截止日期。"})
        return attrs


class ReceivingAccountInputSerializer(serializers.Serializer):
    # Blank sensitive fields on edits retain the encrypted existing values.
    id_number = serializers.CharField(required=False, allow_blank=True, max_length=32)
    bank_card_number = serializers.CharField(required=False, allow_blank=True, max_length=32)
    mobile = serializers.CharField(required=False, allow_blank=True, max_length=20)
    cert_begin_date = serializers.DateField()
    cert_long_term = serializers.BooleanField()
    cert_end_date = serializers.DateField(required=False, allow_null=True)
    bank_name = serializers.CharField(max_length=60)
    bank_province = serializers.CharField(max_length=40)
    bank_city = serializers.CharField(max_length=40)
    consent_accepted = serializers.BooleanField()
    consent_version = serializers.CharField(max_length=32)

    def validate(self, attrs):
        if not attrs["consent_accepted"] or attrs["consent_version"] != CONSENT_VERSION:
            raise serializers.ValidationError({"consent_accepted": "请阅读并同意当前收款资料收集说明。"})
        return attrs


def receiving_account_summary(provider):
    """Safe for staff detail views; never decrypt material or expose channel identifiers."""
    account = ProviderReceivingAccount.objects.filter(provider=provider).first()
    return {
        "materials_saved": bool(account),
        "status_label": "资料已保存，待渠道开通" if account else "未填写收款资料",
        "channel_status": "not_connected",
        "channel_notice": CHANNEL_NOTICE,
        "bank_card_masked": account.bank_card_masked if account else "",
        "bank_name": account.bank_name if account else "",
        "bank_province": account.bank_province if account else "",
        "bank_city": account.bank_city if account else "",
        "mobile_masked": account.mobile_masked if account else "",
        "updated_at": account.updated_at if account else None,
    }


@sensitive_variables()
def receiving_account_data(provider):
    account = ProviderReceivingAccount.objects.filter(provider=provider).first()
    readable = True
    try:
        details = decrypt_details(account) if account else {}
    except ReceivingAccountUnavailable:
        # Keep the masked overview and the ability to withdraw materials available.
        # PUT still fails closed; never replace unreadable data with a blank draft.
        readable = False
        details = {}
    return {
        **receiving_account_summary(provider),
        "collection_enabled": collection_available() and readable,
        "collection_unavailable_reason": (
            "已保存资料暂时无法读取，请联系客服核实，或清除后重新填写。"
            if not readable else "平台准备完成后，你可以在这里填写资料。当前不影响查看订单和账务收入。"
        ),
        "identity_verified": provider.has_verified_identity,
        "real_name": provider.identity_real_name,
        "id_number_masked": account.id_number_masked if account else provider.identity_number_masked,
        "cert_begin_date": details.get("cert_begin_date"),
        "cert_end_date": details.get("cert_end_date"),
        "cert_long_term": details.get("cert_long_term", False),
        "consent_version": CONSENT_VERSION,
        "collection_notice": COLLECTION_NOTICE,
    }


@sensitive_variables()
@transaction.atomic
def save_receiving_account(*, provider, data):
    if not collection_available():
        raise ReceivingAccountUnavailable()
    input_serializer = ReceivingAccountInputSerializer(data=data)
    input_serializer.is_valid(raise_exception=True)
    data = input_serializer.validated_data
    # Same lock order as account closure; do not recreate private data after closure.
    lock_active_user_for_business(provider.user)
    # Lock the always-existing provider, including the initial account creation.
    provider = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    if provider.status != ProviderProfile.Status.APPROVED or not provider.has_verified_identity:
        raise PermissionDenied("请先通过达人实名认证，再填写本人收款资料。")
    if not provider.identity_number_digest or not provider.identity_real_name:
        raise PermissionDenied("实名信息不完整，请联系客服核实后再填写。")
    account = ProviderReceivingAccount.objects.filter(provider=provider).first()
    existing = decrypt_details(account) if account else {}
    payload = {key: value for key, value in data.items() if key in ReceivingDetailsSerializer().fields}
    for key in ("id_number", "bank_card_number", "mobile"):
        supplied = re.sub(r"\s+", "", str(payload.get(key, ""))).upper()
        payload[key] = supplied or existing.get(key, "")
    serializer = ReceivingDetailsSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    details = dict(serializer.validated_data)
    digest = hmac.new(
        settings.SECRET_KEY.encode(), details["id_number"].encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(digest, provider.identity_number_digest):
        raise ValidationError({"id_number": "身份证号码与已通过的实名认证不一致，请核对或联系客服。"})
    details["real_name"] = provider.identity_real_name
    details["cert_begin_date"] = details["cert_begin_date"].isoformat()
    if details.get("cert_end_date"):
        details["cert_end_date"] = details["cert_end_date"].isoformat()
    ProviderReceivingAccount.objects.update_or_create(
        provider=provider,
        defaults={
            "details_ciphertext": encrypt_details(provider.pk, details),
            "id_number_masked": f"{details['id_number'][:4]}**********{details['id_number'][-4:]}",
            "bank_card_masked": f"**** **** **** {details['bank_card_number'][-4:]}",
            "mobile_masked": f"{details['mobile'][:3]}****{details['mobile'][-4:]}",
            "bank_name": details["bank_name"],
            "bank_province": details["bank_province"],
            "bank_city": details["bank_city"],
            "consent_version": CONSENT_VERSION,
            "consented_at": timezone.now(),
        },
    )


@transaction.atomic
def clear_receiving_account(provider):
    ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    ProviderReceivingAccount.objects.filter(provider=provider).delete()
