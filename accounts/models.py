import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

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
