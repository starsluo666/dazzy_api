from django.apps import apps
from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, phone: str, password: str | None = None, **extra_fields):
        if not phone:
            raise ValueError("手机号不能为空")
        # Historical migration models must not query today's media schema.
        # Explicit avatar values (including an intentionally empty one) are preserved.
        if "avatar_object_key" not in extra_fields and self.model._meta.apps is apps:
            from mediafiles.default_avatars import choose_default_avatar

            extra_fields["avatar_object_key"] = choose_default_avatar(using=self.db)
        user = self.model(phone=phone, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone: str, password: str, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if extra_fields.get("is_staff") is not True or extra_fields.get("is_superuser") is not True:
            raise ValueError("超级用户必须设置 is_staff=True 且 is_superuser=True")
        return self.create_user(phone, password, **extra_fields)
