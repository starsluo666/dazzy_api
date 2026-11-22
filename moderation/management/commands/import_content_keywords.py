from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from accounts.nicknames import matching_text
from backoffice.access import resolve_admin_access
from backoffice.models import AdminAuditLog
from moderation.models import ContentKeywordRule


PACKS = {
    "violence": "暴恐词库",
    "sexual": "色情词库",
    "political": "政治词库",
    "abuse": "谩骂词库",
    "illegal": "违法词库",
    "advertising": "广告词库",
}
PACK_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "keyword_packs"


def read_keywords(groups):
    keywords = {}
    for group in groups:
        source = PACK_DIRECTORY / f"{group}.txt"
        try:
            lines = source.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            raise CommandError(f"无法读取 UTF-8 词库：{source.name}") from exc
        for line_no, line in enumerate(lines, 1):
            keyword = line.strip()
            if not keyword:
                continue
            plain = matching_text(keyword)
            if len(keyword) > 20 or not plain or len(plain) > 120:
                raise CommandError(f"{source.name} 第 {line_no} 行不是有效关键词（最多 20 字符）。")
            if plain in keywords:
                raise CommandError(f"{source.name} 第 {line_no} 行存在归一化后重复的关键词。")
            keywords[plain] = (keyword, PACKS[group])
    if not keywords:
        raise CommandError("所选词库没有可导入的关键词。")
    return keywords


def existing_keywords(keywords):
    # Skip the same keyword in any scope rather than reintroduce a disabled or narrower rule.
    return set(ContentKeywordRule.objects.filter(normalized_keyword__in=keywords).values_list("normalized_keyword", flat=True))


def import_operator(phone):
    if not phone:
        raise CommandError("写入时必须通过 --actor-phone 指定有内容审核管理权限的账号。")
    actor = get_user_model().objects.filter(phone=phone, is_active=True, account_status="active").first()
    if actor is None:
        raise CommandError("未找到可用的后台操作账号。")
    try:
        access = resolve_admin_access(actor)
        access.require("content.review.manage")
        if not access.all_data:
            raise PermissionDenied()
    except PermissionDenied as exc:
        raise CommandError("操作账号没有全平台内容审核管理权限。") from exc
    return actor, access.member.organization if access.member else None


class Command(BaseCommand):
    help = "批量导入内置的六组本地关键词。默认只预览，不修改数据库。"

    def add_arguments(self, parser):
        parser.add_argument("--group", action="append", choices=PACKS, help="指定词库，可重复传入；默认选全部。")
        parser.add_argument("--scope", choices=ContentKeywordRule.Scope.values, default=ContentKeywordRule.Scope.BOTH)
        parser.add_argument("--apply", action="store_true", help="确认写入本地词库。")
        parser.add_argument("--actor-phone", help="有全平台内容审核管理权限的后台账号手机号，用于记入操作审计。")

    def handle(self, *args, **options):
        groups = tuple(dict.fromkeys(options["group"] or PACKS))
        scope = options["scope"]
        keywords = read_keywords(groups)
        self.stdout.write("词库：" + "、".join(PACKS[group] for group in groups))
        self.stdout.write(f"作用范围：{ContentKeywordRule.Scope(scope).label}")
        if not options["apply"]:
            existing = existing_keywords(keywords)
            self.stdout.write(f"预览：共 {len(keywords)} 条，拟新增 {len(keywords) - len(existing)} 条，已存在 {len(existing)} 条。未写入数据库。")
            return

        actor, organization = import_operator(options["actor_phone"])
        with transaction.atomic():
            existing = existing_keywords(keywords)
            audits = []
            for plain, (keyword, label) in keywords.items():
                if plain in existing:
                    continue
                rule, created = ContentKeywordRule.objects.get_or_create(
                    scope=scope, normalized_keyword=plain,
                    defaults={"keyword": keyword, "label": label, "enabled": True},
                )
                if created:
                    audits.append(AdminAuditLog(
                        actor=actor, organization=organization, action="content.keyword_rule.create",
                        target_type="content_keyword_rule", target_id=str(rule.pk),
                        after={"keyword": keyword, "scope": scope, "label": label, "enabled": True, "source": "keyword_pack"},
                    ))
            if audits:
                AdminAuditLog.objects.bulk_create(audits)
        self.stdout.write(self.style.SUCCESS(f"导入完成：新增 {len(audits)} 条，跳过已有规则 {len(keywords) - len(audits)} 条。"))
