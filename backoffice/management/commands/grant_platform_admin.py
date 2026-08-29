from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from backoffice.models import AdminRole, Organization, OrganizationMember


class Command(BaseCommand):
    help = "按手机号授予乐搭伴平台运营管理员权限（幂等）。"

    def add_arguments(self, parser):
        parser.add_argument("phone", help="已注册用户手机号")

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            user = User.objects.get(phone=options["phone"])
        except User.DoesNotExist as exc:
            raise CommandError("未找到该手机号对应的用户，请先注册账号。") from exc
        organization, _ = Organization.objects.get_or_create(
            code="dazzy-platform",
            defaults={
                "name": "乐搭伴运营平台",
                "organization_type": Organization.Type.PLATFORM,
            },
        )
        role, _ = AdminRole.objects.update_or_create(
            organization=organization,
            code="platform-admin",
            defaults={
                "name": "平台管理员",
                "permissions": [
                    "dashboard.view",
                    "user.view",
                    "user.status.manage",
                    "user.risk.manage",
                    "provider.view",
                    "provider.review",
                    "provider.manage",
                    "provider.credit.adjust",
                    "service_category.view",
                    "service_category.manage",
                    "activity.view",
                    "activity.review",
                    "activity.manage",
                    "activity_category.view",
                    "activity_category.manage",
                    "activity_report.view",
                    "activity_report.manage",
                    "activity_finance.view",
                    "activity_after_sales.manage",
                    "order.fulfillment.view",
                    "order.support_note.add",
                    "order.after_sales.view",
                    "order.after_sales.review",
                    "organization.manage",
                    "audit.view",
                ],
                "data_scope": AdminRole.DataScope.ALL,
                "is_system": True,
            },
        )
        OrganizationMember.objects.update_or_create(
            user=user,
            organization=organization,
            defaults={"role": role, "is_active": True},
        )
        self.stdout.write(self.style.SUCCESS(f"已授予 {user} 平台管理员权限。"))
