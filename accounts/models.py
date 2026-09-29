import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from .managers import UserManager


class User(AbstractUser):
    class Gender(models.TextChoices):
        UNSPECIFIED = "unspecified", "未设置"
        MALE = "male", "男"
        FEMALE = "female", "女"

    class VerificationStatus(models.TextChoices):
        UNVERIFIED = "unverified", "未认证"
        PENDING = "pending", "认证中"
        VERIFIED = "verified", "已认证"
        REJECTED = "rejected", "认证未通过"

    class AccountStatus(models.TextChoices):
        ACTIVE = "active", "正常"
        RESTRICTED = "restricted", "受限"
        SUSPENDED = "suspended", "停用"
        CLOSURE_PENDING = "closure_pending", "注销申请处理中"
        CLOSED = "closed", "已注销"

    username = None
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    phone = models.CharField("手机号", max_length=20, unique=True)
    nickname = models.CharField("昵称", max_length=30, blank=True)
    gender = models.CharField("性别", max_length=16, choices=Gender, default=Gender.UNSPECIFIED)
    birth_date = models.DateField("生日", null=True, blank=True)
    avatar_object_key = models.CharField("头像对象键", max_length=512, blank=True)
    verification_status = models.CharField(
        "实名认证状态",
        max_length=16,
        choices=VerificationStatus,
        default=VerificationStatus.UNVERIFIED,
    )
    account_status = models.CharField(
        "账号状态", max_length=16, choices=AccountStatus, default=AccountStatus.ACTIVE
    )
    auth_version = models.PositiveIntegerField("认证版本", default=1, editable=False)

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS: list[str] = []
    objects = UserManager()

    class Meta:
        db_table = "accounts_user"
        verbose_name = "用户"
        verbose_name_plural = "用户"

    def __str__(self) -> str:
        return self.nickname or self.phone


class AccountClosureRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "等待注销"
        BLOCKED = "blocked", "未结业务暂停处理"
        CANCELLED = "cancelled", "登录撤销"
        COMPLETED = "completed", "已完成注销"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="closure_requests",
    )
    requested_at = models.DateTimeField("申请时间", default=timezone.now)
    execute_after = models.DateTimeField("最早注销时间")
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    finished_at = models.DateTimeField("完成或撤销时间", null=True, blank=True)
    checked_at = models.DateTimeField("最近检查时间", null=True, blank=True)
    blocking_items = models.JSONField("待处理业务", default=list, blank=True)

    class Meta:
        db_table = "accounts_closure_request"
        verbose_name = "账号注销申请"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=("user",), condition=models.Q(status__in=("pending", "blocked")),
                name="uniq_open_account_closure",
            ),
        ]
        indexes = [models.Index(fields=("status", "execute_after"))]


class WechatOfficialAccountIdentity(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wechat_official_account_identities",
        verbose_name="用户",
    )
    app_id = models.CharField("服务号 AppID", max_length=32)
    openid = models.CharField("服务号 OpenID", max_length=128)
    unionid = models.CharField("微信 UnionID", max_length=128, blank=True)
    authorized_at = models.DateTimeField("最近授权时间")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_wechat_official_identity"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "app_id"),
                name="uniq_wechat_official_user_app",
            ),
            models.UniqueConstraint(
                fields=("app_id", "openid"),
                name="uniq_wechat_official_app_openid",
            ),
        ]
        verbose_name = "微信服务号身份"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.user_id} / {self.app_id}"


class WechatMiniProgramIdentity(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wechat_mini_program_identities",
        verbose_name="用户",
    )
    app_id = models.CharField("小程序 AppID", max_length=32)
    openid = models.CharField("小程序 OpenID", max_length=128)
    unionid = models.CharField("微信 UnionID", max_length=128, blank=True)
    authorized_at = models.DateTimeField("最近授权时间")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_wechat_mini_program_identity"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "app_id"),
                name="uniq_wechat_mini_user_app",
            ),
            models.UniqueConstraint(
                fields=("app_id", "openid"),
                name="uniq_wechat_mini_app_openid",
            ),
        ]
        verbose_name = "微信小程序身份"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.user_id} / {self.app_id}"


class WechatLoginIdentity(models.Model):
    class Channel(models.TextChoices):
        OFFICIAL_ACCOUNT = "official_account", "微信内网页"
        MOBILE_APP = "mobile_app", "原生 App"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wechat_login_identities",
        verbose_name="用户",
    )
    channel = models.CharField("登录渠道", max_length=24, choices=Channel)
    app_id = models.CharField("微信 AppID", max_length=32)
    openid = models.CharField("微信 OpenID", max_length=128)
    unionid = models.CharField("微信 UnionID", max_length=128, blank=True)
    authorized_at = models.DateTimeField("最近授权时间")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_wechat_login_identity"
        constraints = [
            models.UniqueConstraint(
                fields=("app_id", "openid"), name="uniq_wechat_login_app_openid"
            ),
            models.UniqueConstraint(
                fields=("user", "app_id"), name="uniq_wechat_login_user_app"
            ),
        ]
        verbose_name = "微信登录身份"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.user_id} / {self.channel} / {self.app_id}"


class WechatUnionIdentity(models.Model):
    """Single owner across apps in the configured WeChat Open Platform namespace."""

    unionid = models.CharField("微信 UnionID", max_length=128, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="wechat_union_identities", verbose_name="用户",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_wechat_union_identity"
        verbose_name = "微信跨端身份归属"
        verbose_name_plural = verbose_name
