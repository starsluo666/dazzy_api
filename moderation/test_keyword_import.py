from collections import Counter
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from backoffice.models import AdminAuditLog
from .management.commands.import_content_keywords import PACKS, read_keywords
from .models import ContentKeywordRule


class KeywordPackTests(SimpleTestCase):
    def test_every_pack_has_one_hundred_distinct_terms_short_enough_for_tencent(self):
        keywords = read_keywords(PACKS)
        self.assertEqual(Counter(label for _, label in keywords.values()), dict.fromkeys(PACKS.values(), 100))
        self.assertTrue(all(1 <= len(keyword) <= 20 for keyword, _ in keywords.values()))

    def test_file_with_normalized_duplicate_is_rejected(self):
        with patch("pathlib.Path.read_text", return_value="私下转账\n私下·转账\n"):
            with self.assertRaisesRegex(CommandError, "第 2 行.*重复"):
                read_keywords(["advertising"])


class KeywordImportTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_superuser(phone="13900002001", password="test-only-password")

    def import_command(self, *args):
        output = StringIO()
        call_command("import_content_keywords", *args, stdout=output)
        return output.getvalue()

    def test_default_is_preview_and_never_writes_rules_or_audit(self):
        self.assertIn("拟新增 600 条", self.import_command())
        self.assertFalse(ContentKeywordRule.objects.exists())
        self.assertFalse(AdminAuditLog.objects.exists())

    def test_apply_requires_a_content_manager(self):
        ordinary = get_user_model().objects.create_user(phone="13900002002")
        with self.assertRaisesMessage(CommandError, "--actor-phone"):
            self.import_command("--apply")
        with self.assertRaisesMessage(CommandError, "没有全平台内容审核管理权限"):
            self.import_command("--apply", "--actor-phone", ordinary.phone)
        self.assertFalse(ContentKeywordRule.objects.exists())

    def test_all_six_groups_can_be_applied_as_one_batch(self):
        self.assertIn("新增 600 条", self.import_command("--apply", "--actor-phone", self.operator.phone))
        rules = ContentKeywordRule.objects.filter(scope="both", enabled=True)
        self.assertEqual(Counter(rules.values_list("label", flat=True)), dict.fromkeys(PACKS.values(), 100))
        self.assertEqual(AdminAuditLog.objects.filter(actor=self.operator, target_type="content_keyword_rule").count(), 600)

    def test_repeated_import_is_idempotent_and_keeps_existing_disabled_rule_in_any_scope(self):
        original = ContentKeywordRule.objects.create(
            keyword="加微信走私单", normalized_keyword="加微信走私单", scope="nickname",
            label="人工规则", enabled=False,
        )
        args = ("--group", "advertising", "--scope", "review", "--apply", "--actor-phone", self.operator.phone)
        self.assertIn("新增 99 条，跳过已有规则 1 条", self.import_command(*args))
        self.assertIn("新增 0 条，跳过已有规则 100 条", self.import_command(*args))
        original.refresh_from_db()
        self.assertFalse(original.enabled)
        self.assertEqual(original.label, "人工规则")
        self.assertEqual(ContentKeywordRule.objects.filter(normalized_keyword=original.normalized_keyword).count(), 1)
        self.assertEqual(ContentKeywordRule.objects.filter(scope="review", label="广告词库", enabled=True).count(), 99)
        self.assertEqual(AdminAuditLog.objects.filter(actor=self.operator, action="content.keyword_rule.create").count(), 99)
