"""Read-only local configuration check; never send a verification message."""

from django.core.management.base import BaseCommand, CommandError

from accounts.tencent_sms import TEMPLATE_SETTINGS, SmsConfig, SmsDeliveryUnavailable


class Command(BaseCommand):
    help = "检查腾讯云验证码短信配置是否完整；不发送短信、不验证云端权限、不输出密钥。"

    def handle(self, *args, **options):
        failures, configs = [], []
        for purpose in TEMPLATE_SETTINGS:
            try:
                configs.append((purpose, SmsConfig.load(purpose=purpose)))
            except SmsDeliveryUnavailable:
                failures.append(purpose)
        if failures:
            raise CommandError(
                f"短信配置未就绪，用途：{','.join(failures)}。"
                "请检查 TENCENT_SMS_ENABLED、成对密钥、SDK_APP_ID、SIGN_NAME、"
                "对应场景模板或默认 TEMPLATE_ID、TEMPLATE_PARAMS，以及验证码有效期和限频配置；"
                "详见 README 的腾讯云验证码短信章节。"
            )
        config = configs[0][1]
        self.stdout.write(self.style.SUCCESS(
            f"本地短信配置完整（已检查全部 {len(configs)} 个用途，"
            f"参数顺序：{','.join(config.params)}）。"
            "未发送短信；签名/模板审核、账号权限、套餐余额和手机实际接收仍需联调确认。"
        ))
