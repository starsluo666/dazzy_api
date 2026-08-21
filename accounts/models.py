import uuid

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
