from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from backoffice.models import AdminAuditLog
from . import gateway, local_rules, services
from .models import ContentKeywordRule, ContentReview
from .tests import decide, fixture


class LocalRuleTests(TransactionTestCase):
    def setUp(self):
        fixture(self)

    def test_platform_can_add_scope_edit_and_disable_normalized_rule(self):
        url = reverse("content-keyword-rules")
        data = {"keyword": "测试成交词", "scope": "review", "label": "引流提示", "enabled": True}
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 403)
        self.client.force_authenticate(self.admin)
        created = self.client.post(url, data, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        rule_id = created.data["data"]["id"]
        duplicate = self.client.post(url, {**data, "keyword": "测试·成交词"}, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)
        self.assertEqual(self.client.get(url, {"q": "引流"}).data["data"]["pagination"]["total"], 1)
        snapshot = {"text": {"内容": "请先测试 成交词后确认"}}
        self.assertEqual(local_rules.screen("nickname", snapshot), [])
        self.assertEqual(local_rules.screen("review", snapshot)[0]["rule_id"], rule_id)
        detail = reverse("content-keyword-rule", args=(rule_id,))
        updated = self.client.patch(detail, {"scope": "both", "label": "新说明"}, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(local_rules.screen("nickname", snapshot)[0]["label"], "新说明")
        disabled = self.client.patch(detail, {"enabled": False}, format="json")
        self.assertEqual(disabled.status_code, 200, disabled.data)
        self.assertEqual(local_rules.screen("review", snapshot), [])
        self.assertEqual(AdminAuditLog.objects.filter(target_type="content_keyword_rule", target_id=str(rule_id)).count(), 3)

    @override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=True,
                       TENCENT_CONTENT_REVIEW_SECRET_ID="offline-id", TENCENT_CONTENT_REVIEW_SECRET_KEY="offline-key")
    def test_local_match_is_preserved_and_cloud_pass_routes_to_one_manual_decision(self):
        rule = ContentKeywordRule.objects.create(keyword="测试串", normalized_keyword="测试串", scope="nickname")
        review = services.submit("nickname", self.user, candidate="测试 串小雨")
        self.assertEqual(review.status, "queued")
        rule.enabled = False
        rule.save()
        with patch.object(gateway, "detect_text", return_value={"suggestion": "Pass"}) as detect:
            self.assertTrue(services.process_one(review.pk))
        detect.assert_called_once()
        review.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(review.status, "manual")
        self.assertEqual(review.machine_suggestion, "Pass")
        self.assertEqual(self.user.nickname, "原昵称")
        self.client.force_authenticate(self.admin)
        detail = self.client.get(reverse("content-review-detail", args=(review.pk,)))
        self.assertEqual(detail.data["data"]["local_findings"][0]["keyword"], "测试串")
        decide(review, self.admin)
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "测试 串小雨")

    @override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=True,
                       TENCENT_CONTENT_REVIEW_SECRET_ID="", TENCENT_CONTENT_REVIEW_SECRET_KEY="")
    def test_missing_credentials_immediately_route_to_manual_and_unchanged_name_skips_submission(self):
        self.client.force_authenticate(self.user)
        url = "/api/v1/users/me/"
        unchanged = self.client.patch(url, {"nickname": "原昵称", "gender": "female"}, format="json")
        self.assertEqual(unchanged.status_code, 200, unchanged.data)
        self.assertFalse(ContentReview.objects.exists())
        submitted = self.client.patch(url, {"nickname": "等待复核"}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.assertEqual(submitted.data["data"]["nickname"], "原昵称")
        self.assertEqual(ContentReview.objects.get().status, "manual")
