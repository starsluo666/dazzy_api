import re

from django.contrib.auth import authenticate, password_validation
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from mediafiles.services import build_media_url

from .models import User
from .services import (
    auth_client_identifier,
    clear_auth_failures,
    ensure_auth_attempt_allowed,
    record_auth_failure,
    verify_sms_code,
)


PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")


def validate_phone(value: str) -> str:
    normalized = value.strip()
    if not PHONE_PATTERN.fullmatch(normalized):
        raise serializers.ValidationError("请输入正确的中国大陆手机号。")
    return normalized


def validate_password(value: str) -> str:
    if not 8 <= len(value) <= 20:
        raise serializers.ValidationError("密码长度须为 8–20 位。")
    password_validation.validate_password(value)
    return value


class UserSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "public_id", "phone", "nickname", "gender", "birth_date", "avatar_url",
            "account_status",
        )
        read_only_fields = ("public_id", "phone", "avatar_url", "account_status")

    def get_avatar_url(self, obj) -> str | None:
        return build_media_url(obj.avatar_object_key)

    def validate_nickname(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("昵称不能为空。")
        return normalized

    def validate_birth_date(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("生日不能晚于今天。")
        return value


class SmsCodeRequestSerializer(serializers.Serializer):
    phone = serializers.CharField(validators=[validate_phone])
    purpose = serializers.ChoiceField(choices=("register", "login", "reset_password"))

    def validate(self, attrs):
        exists = User.objects.filter(phone=attrs["phone"]).exists()
        if attrs["purpose"] == "register" and exists:
            raise serializers.ValidationError({"phone": "该手机号已注册，请直接登录。"})
        if attrs["purpose"] != "register" and not exists:
            raise serializers.ValidationError({"phone": "该手机号尚未注册。"})
        return attrs


class RegisterSerializer(serializers.Serializer):
    phone = serializers.CharField(validators=[validate_phone])
    code = serializers.CharField(min_length=6, max_length=6)
    password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_phone(self, value):
        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("该手机号已注册。")
        return value

    def create(self, validated_data):
        phone = validated_data["phone"]
        client_identifier = auth_client_identifier(self.context.get("request"))
        ensure_auth_attempt_allowed(
            phone=phone, purpose="register", client_identifier=client_identifier
        )
        try:
            verify_sms_code(phone=phone, purpose="register", code=validated_data["code"])
        except serializers.ValidationError:
            record_auth_failure(
                phone=phone, purpose="register", client_identifier=client_identifier
            )
            raise
        clear_auth_failures(
            phone=phone, purpose="register", client_identifier=client_identifier
        )
        try:
            # Keep the unique-phone IntegrityError inside a savepoint so the outer
            # RegisterView transaction remains usable for a clean 400 response.
            with transaction.atomic():
                return User.objects.create_user(
                    phone=phone,
                    password=validated_data["password"],
                    nickname=f"用户{phone[-4:]}",
                )
        except IntegrityError as exc:
            raise serializers.ValidationError({"phone": "该手机号已注册。"}) from exc


class PasswordLoginSerializer(serializers.Serializer):
    phone = serializers.CharField(validators=[validate_phone])
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        client_identifier = auth_client_identifier(self.context.get("request"))
        ensure_auth_attempt_allowed(
            phone=attrs["phone"],
            purpose="password_login",
            client_identifier=client_identifier,
        )
        user = authenticate(
            request=self.context.get("request"),
            phone=attrs["phone"],
            password=attrs["password"],
        )
        if not user:
            record_auth_failure(
                phone=attrs["phone"],
                purpose="password_login",
                client_identifier=client_identifier,
            )
            raise serializers.ValidationError("手机号或密码错误。")
        if user.account_status != User.AccountStatus.ACTIVE or not user.is_active:
            raise serializers.ValidationError("账号当前不可用，请联系客服。")
        attrs["user"] = user
        clear_auth_failures(
            phone=attrs["phone"],
            purpose="password_login",
            client_identifier=client_identifier,
        )
        return attrs


class SmsLoginSerializer(serializers.Serializer):
    phone = serializers.CharField(validators=[validate_phone])
    code = serializers.CharField(min_length=6, max_length=6)

    def validate(self, attrs):
        try:
            user = User.objects.get(phone=attrs["phone"])
        except User.DoesNotExist as exc:
            raise serializers.ValidationError({"phone": "该手机号尚未注册。"}) from exc
        if user.account_status != User.AccountStatus.ACTIVE or not user.is_active:
            raise serializers.ValidationError("账号当前不可用，请联系客服。")
        client_identifier = auth_client_identifier(self.context.get("request"))
        ensure_auth_attempt_allowed(
            phone=attrs["phone"],
            purpose="sms_login",
            client_identifier=client_identifier,
        )
        try:
            verify_sms_code(phone=attrs["phone"], purpose="login", code=attrs["code"])
        except serializers.ValidationError:
            record_auth_failure(
                phone=attrs["phone"],
                purpose="sms_login",
                client_identifier=client_identifier,
            )
            raise
        clear_auth_failures(
            phone=attrs["phone"],
            purpose="sms_login",
            client_identifier=client_identifier,
        )
        attrs["user"] = user
        return attrs


class ResetPasswordSerializer(serializers.Serializer):
    phone = serializers.CharField(validators=[validate_phone])
    code = serializers.CharField(min_length=6, max_length=6)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate(self, attrs):
        try:
            attrs["user"] = User.objects.get(phone=attrs["phone"])
        except User.DoesNotExist as exc:
            raise serializers.ValidationError({"phone": "该手机号尚未注册。"}) from exc
        return attrs

    def save(self, **kwargs):
        phone = self.validated_data["phone"]
        client_identifier = auth_client_identifier(self.context.get("request"))
        ensure_auth_attempt_allowed(
            phone=phone,
            purpose="reset_password",
            client_identifier=client_identifier,
        )
        try:
            verify_sms_code(phone=phone, purpose="reset_password", code=self.validated_data["code"])
        except serializers.ValidationError:
            record_auth_failure(
                phone=phone,
                purpose="reset_password",
                client_identifier=client_identifier,
            )
            raise
        clear_auth_failures(
            phone=phone,
            purpose="reset_password",
            client_identifier=client_identifier,
        )
        user = self.validated_data["user"]
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=("password",))
        return invalidate_user_sessions(user)


def invalidate_user_sessions(user: User) -> User:
    user.auth_version = F("auth_version") + 1
    user.save(update_fields=("auth_version",))
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)
    user.refresh_from_db(fields=("auth_version",))
    return user


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate(self, attrs):
        user = self.context["request"].user
        if not user.check_password(attrs["current_password"]):
            raise serializers.ValidationError({"current_password": "当前密码不正确。"})
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError({"new_password": "新密码不能与当前密码相同。"})
        attrs["user"] = user
        return attrs

    def save(self, **kwargs):
        user = self.validated_data["user"]
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=("password",))
        return invalidate_user_sessions(user)


class LogoutOtherSessionsSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = self.context["request"].user
        if not user.check_password(attrs["current_password"]):
            raise serializers.ValidationError({"current_password": "当前密码不正确。"})
        attrs["user"] = user
        return attrs

    def save(self, **kwargs):
        return invalidate_user_sessions(self.validated_data["user"])


class CloseAccountSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)

    def save(self, **kwargs):
        user = self.context["request"].user
        if not user.check_password(self.validated_data["current_password"]):
            raise serializers.ValidationError({"current_password": "当前密码不正确。"})
        user.account_status = User.AccountStatus.CLOSED
        user.is_active = False
        user.auth_version = F("auth_version") + 1
        user.save(update_fields=("account_status", "is_active", "auth_version"))
        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)
        return user


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()
