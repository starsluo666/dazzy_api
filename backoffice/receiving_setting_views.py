import re
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import client_ip, resolve_admin_access
from .models import AdminAuditLog, ReceivingWithdrawalSetting
from .receiving_settings import FORM_FIELDS, build_cash_config, current_setting, form_values, settings_payload


class ConfigurationConflict(APIException):
    status_code = 409
    default_detail = "配置已被其他管理员修改，请重新加载并核对后再保存。"


class ReceivingSettingSerializer(serializers.Serializer):
    cash_type = serializers.ChoiceField(choices=("T1", "D1"))
    out_fee_acct_type = serializers.ChoiceField(choices=("01", "02", "05"))
    fix_amt = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal("0"), allow_null=True)
    fee_rate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"), allow_null=True)
    weekday_fix_amt = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal("0"), allow_null=True)
    weekday_fee_rate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"), allow_null=True)
    revision = serializers.IntegerField(min_value=0)
    confirmed = serializers.BooleanField()
    reason = serializers.CharField(max_length=200, allow_blank=False, trim_whitespace=True)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise ValidationError({"detail": "仅允许修改提现周期、费用及平台手续费账户类型。"})
        if data.get("confirmed") is not True:
            raise ValidationError({"confirmed": "请确认费率已与汇付核实，且仅用于后续开户配置。"})
        return super().to_internal_value(data)

    def validate(self, attrs):
        if attrs["fix_amt"] is None and attrs["fee_rate"] is None:
            raise ValidationError({"fix_amt": "固定费用与费率至少填写一项；免手续费请明确填写 0。"})
        if attrs["cash_type"] != "D1" and any(attrs[key] is not None for key in ("weekday_fix_amt", "weekday_fee_rate")):
            raise ValidationError({"cash_type": "仅 D1 可配置工作日费用，切换 T1 请清空工作日费用。"})
        if not re.fullmatch(r"[0-9]{1,18}", settings.HUIFU_MERCHANT_ID.strip()):
            raise ValidationError({"detail": "请先由运维配置有效的 HUIFU_MERCHANT_ID，才能绑定平台手续费承担方。"})
        build_cash_config(attrs, settings.HUIFU_MERCHANT_ID.strip())
        return attrs


class ReceivingWithdrawalSettingView(APIView):
    permission_classes = [IsAuthenticated]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        return response

    @staticmethod
    def require_access(request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        if not access.all_data:
            raise PermissionDenied("只有具有全局运营配置权限的平台管理员可以管理收款与提现配置。")
        return access

    def get(self, request):
        self.require_access(request)
        return Response({"data": settings_payload(current_setting())})

    def put(self, request):
        access = self.require_access(request)
        serializer = ReceivingSettingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            with transaction.atomic():
                setting = ReceivingWithdrawalSetting.objects.select_for_update().filter(singleton_key="default").first()
                revision = setting.revision if setting else 0
                if revision != data["revision"]:
                    raise ConfigurationConflict()
                before = {"revision": revision, "source": "admin" if setting else "environment_or_unconfigured"}
                if setting:
                    before.update(form_values(setting))
                else:
                    # Only safe form fields; never audit raw env JSON or credentials.
                    previous = settings_payload(None)
                    before.update(previous["form"], source=previous["source"])
                    setting = ReceivingWithdrawalSetting(singleton_key="default")
                for key in FORM_FIELDS:
                    setattr(setting, key, data[key])
                setting.fee_bearer_id = settings.HUIFU_MERCHANT_ID.strip()
                setting.revision = revision + 1
                setting.updated_by = request.user
                setting.save()
                AdminAuditLog.objects.create(
                    actor=request.user, organization=access.member.organization if access.member else None,
                    action="operations.receiving_withdrawal.update", target_type="operation_setting", target_id="receiving_withdrawal",
                    before=before, after={**form_values(setting), "revision": setting.revision, "source": "admin", "fee_bearer": "platform", "reason": data["reason"]},
                    ip_address=client_ip(request),
                )
        except IntegrityError:
            # Two first publications raced on the unique singleton key. Do not
            # overwrite the winner or commit a setting without its audit log.
            raise ConfigurationConflict() from None
        return Response({"data": settings_payload(setting)})
