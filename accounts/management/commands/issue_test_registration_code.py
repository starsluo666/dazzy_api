"""Issue a short-lived registration code from a trusted server terminal only."""

import re
import secrets

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from accounts.services import _code_attempt_key, _code_key


class Command(BaseCommand):
    help = "为已配置的测试手机号签发一次性注册验证码；不通过公开短信接口发放。"

    def add_arguments(self, parser):
        parser.add_argument("phone", help="SMS_TEST_REGISTRATION_PHONES 白名单中的未注册手机号")

    def handle(self, *args, **options):
        phone = options["phone"].strip()
        if not re.fullmatch(r"1[3-9]\d{9}", phone):
            raise CommandError("请输入有效的中国大陆手机号。")
        if phone not in settings.SMS_TEST_REGISTRATION_PHONES:
            raise CommandError("手机号不在 SMS_TEST_REGISTRATION_PHONES 测试白名单中。")
        if User.objects.filter(phone=phone).exists():
            raise CommandError("该手机号已注册，请使用登录或密码找回流程。")

        code = f"{secrets.randbelow(1_000_000):06d}"
        ttl = settings.SMS_CODE_TTL_SECONDS
        cache.set(_code_key(phone, "register"), code, ttl)
        cache.delete(_code_attempt_key(phone, "register"))
        self.stdout.write(f"测试注册验证码：{code}（{ttl} 秒内有效、验证后即失效；请勿分享）")
