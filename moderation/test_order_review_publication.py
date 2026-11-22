from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mediafiles.models import MediaAsset
from notifications.models import UserNotification
from orders.models import ProviderOrder, ProviderOrderReview
from . import gateway, services
from .models import ContentReview
from .tests import decide, fixture


@override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=True,
                   TENCENT_CONTENT_REVIEW_SECRET_ID="offline-id", TENCENT_CONTENT_REVIEW_SECRET_KEY="offline-key")
class OrderReviewPublicationTests(TransactionTestCase):
    def setUp(self):
        fixture(self)

    def order(self, suffix):
        now = timezone.now()
        return ProviderOrder.objects.create(
            order_no=f"DZY-PUBLICATION-{suffix}", customer=self.other,
            provider=self.provider, service=self.service, provider_name_snapshot="测试达人",
            service_name_snapshot="桌球", billing_type_snapshot="hourly", unit_price_amount=10000,
            starts_at=now - timedelta(hours=3), ends_at=now - timedelta(hours=1), duration_minutes=120,
            meeting_address="测试地点", contact_name="测试", contact_phone=self.other.phone,
            service_fee_amount=20000, payable_amount=20000, payment_expires_at=now,
            status="pending_review", review_expires_at=now + timedelta(days=1),
        )

    def submit(self, suffix="TEST", *, rating=1):
        order = self.order(suffix)
        self.client.force_authenticate(self.other)
        response = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/review/",
            {"rating": rating, "content": "如实记录本次服务体验。"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")
        self.assertEqual(order.review.audit_status, "pending")
        return order.review, ContentReview.objects.get(kind="review", target_id=order.review.pk)

    def public_reviews(self):
        url = reverse("provider-reviews", args=(self.provider.user.public_id,))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["items"]

    def notifications(self):
        return UserNotification.objects.filter(
            recipient=self.other, event_type=UserNotification.EventType.ORDER_REVIEW_RESULT,
        )

    @override_settings(CONTENT_REVIEW_ENABLED=False, TENCENT_CONTENT_REVIEW_ENABLED=False)
    def test_stars_only_publish_equally_without_creating_a_review_task(self):
        self.client.force_authenticate(self.other)
        for rating, content in ((1, "   "), (5, "")):
            order = self.order(str(rating))
            response = self.client.post(
                f"/api/v1/provider-orders/{order.order_no}/review/",
                {"rating": rating, "content": content, "image_ids": []}, format="json",
            )
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["data"]["review"]["audit_status"], "approved")
        self.assertFalse(ContentReview.objects.filter(kind="review").exists())
        self.assertCountEqual([review["rating"] for review in self.public_reviews()], [1, 5])
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.rating, Decimal("3.00"))

    def test_pass_publishes_legitimate_low_rating_and_notifies_once(self):
        review, content = self.submit()
        self.assertEqual(self.public_reviews(), [])
        own = self.client.get("/api/v1/users/me/provider-reviews/")
        self.assertEqual(own.data["data"]["items"][0]["audit_status"], "pending")
        with patch.object(gateway, "detect_text", return_value={"suggestion": "Pass"}):
            self.assertTrue(services.process_one(content.pk))
            self.assertFalse(services.process_one(content.pk))
        review.refresh_from_db()
        self.provider.refresh_from_db()
        self.assertEqual(review.audit_status, "approved")
        self.assertEqual(self.public_reviews()[0]["rating"], 1)
        self.assertEqual(self.provider.rating, Decimal("1.00"))
        self.assertEqual(self.notifications().get().title, "评价审核通过")

    def test_stars_only_can_be_hidden_and_restored_with_content_review_enabled(self):
        order = self.order("RESTORE")
        self.client.force_authenticate(self.other)
        response = self.client.post(f"/api/v1/provider-orders/{order.order_no}/review/", {"rating": 1}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(ContentReview.objects.filter(kind="review").exists())
        self.client.force_authenticate(self.admin)
        url = reverse("backoffice-provider-order-review-action", args=(order.order_no,))
        hidden = self.client.post(url, {"action": "hide", "reason": "测试屏蔽"}, format="json")
        self.assertEqual(hidden.status_code, 200, hidden.data)
        self.assertEqual(self.public_reviews(), [])
        restored = self.client.post(url, {"action": "restore"}, format="json")
        self.assertEqual(restored.status_code, 200, restored.data)
        self.assertEqual(self.public_reviews()[0]["rating"], 1)

    def test_image_only_review_is_not_treated_as_stars_only(self):
        order = self.order("IMAGE")
        self.client.force_authenticate(self.other)
        image = MediaAsset.objects.create(
            owner=self.other, category="review_image", scope="public", status="uploaded", object_key="review/example.webp",
        )
        response = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/review/",
            {"rating": 5, "image_ids": [str(image.pk)]}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        review = ContentReview.objects.get(kind="review")
        self.assertEqual(list(review.items.values_list("kind", flat=True)), ["image"])
        self.assertEqual(self.public_reviews(), [])
        with patch.object(gateway, "detect_image", return_value={"suggestion": "Pass"}) as detect:
            self.assertTrue(services.process_one(review.pk))
        detect.assert_called_once()
        self.assertEqual(self.public_reviews()[0]["rating"], 5)

    def test_machine_review_or_failure_stays_pending_until_manual_decision(self):
        for i, suggestion in enumerate(("Review", None)):
            review, content = self.submit(str(i), rating=4)
            options = {"return_value": {"suggestion": suggestion}} if suggestion else {
                "side_effect": gateway.DetectionUnavailable("test_unavailable"),
            }
            with patch.object(gateway, "detect_text", **options):
                self.assertTrue(services.process_one(content.pk))
            content.refresh_from_db()
            review.refresh_from_db()
            self.assertEqual(content.status, "manual")
            self.assertEqual(review.audit_status, "pending")
            self.assertEqual(len(self.public_reviews()), i)
            decide(content, self.admin)
            review.refresh_from_db()
            self.assertEqual(review.audit_status, "approved")
            self.assertEqual(review.audited_by, self.admin)
            self.assertEqual(len(self.public_reviews()), i + 1)

    def test_block_does_not_publish_and_only_content_recheck_can_reverse(self):
        review, content = self.submit(rating=5)
        with patch.object(gateway, "detect_text", return_value={"suggestion": "Block"}):
            services.process_one(content.pk)
        review.refresh_from_db()
        self.assertEqual(review.audit_status, "rejected")
        self.assertFalse(review.is_visible)
        self.assertEqual(self.public_reviews(), [])
        self.assertEqual(self.notifications().get().title, "评价审核未通过")
        self.client.force_authenticate(self.admin)
        url = reverse("backoffice-provider-order-review-action", args=(review.order.order_no,))
        self.assertEqual(self.client.post(url, {"action": "restore"}).status_code, 400)
        content.refresh_from_db()
        decide(content, self.admin, "approve")
        self.assertEqual(self.public_reviews()[0]["rating"], 5)
        self.assertEqual(self.notifications().count(), 2)

    @override_settings(CONTENT_REVIEW_ENABLED=False)
    def test_manual_fallback_when_detection_is_off_and_rejection_is_visible_to_author(self):
        review, content = self.submit()
        self.assertEqual(content.status, "manual")
        self.client.force_authenticate(self.admin)
        listing = self.client.get(reverse("content-review-list"), {"q": review.order.order_no})
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual([item["id"] for item in listing.data["data"]["items"]], [content.pk])
        rejected = self.client.post(
            reverse("content-review-detail", args=(content.pk,)),
            {"action": "reject", "revision": content.revision, "reason": "已逐项核实内容"}, format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        review.refresh_from_db()
        self.assertEqual(review.audit_status, "rejected")
        self.assertNotIn("逐项核实", review.audit_rejection_reason)
        self.assertEqual(self.notifications().get().title, "评价审核未通过")
        self.assertEqual(self.public_reviews(), [])

    def test_legacy_pending_cases_are_reconciled_without_bypassing_content_review(self):
        for suffix in ("APPROVED", "REJECTED", "MISSING", "STALE"):
            order = self.order(suffix)
            obj = ProviderOrderReview.objects.create(
                order=order, customer=self.other, provider=self.provider, rating=4, content="既有评价。",
            )
            if suffix != "MISSING":
                content = services.submit("review", obj, force=True)
                ContentReview.objects.filter(pk=content.pk).update(
                    status="rejected" if suffix == "REJECTED" else "approved",
                )
                if suffix == "STALE":
                    ProviderOrderReview.objects.filter(pk=obj.pk).update(content="已变更的评价。")
        self.assertEqual(len(services.legacy_order_review_ids()), 4)
        for pk in services.legacy_order_review_ids():
            self.assertTrue(services.reconcile_legacy_order_review(pk))
        self.assertEqual(services.legacy_order_review_ids(), [])
        statuses = dict(ProviderOrderReview.objects.values_list("order__order_no", "audit_status"))
        self.assertEqual(statuses, {
            "DZY-PUBLICATION-APPROVED": "approved", "DZY-PUBLICATION-REJECTED": "rejected",
            "DZY-PUBLICATION-MISSING": "pending", "DZY-PUBLICATION-STALE": "pending",
        })
        self.assertEqual(ContentReview.objects.filter(is_current=True, status="manual").count(), 2)
        self.assertEqual(self.notifications().count(), 2)
