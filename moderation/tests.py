import base64
from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.test import TestCase, TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from activities.models import Activity, ActivityParticipation
from mediafiles.models import MediaAsset
from providers.models import ProviderProfile, ProviderService, ServiceCategory
from providers.tests import make_provider_eligible
from . import gateway, services
from .models import ContentReview as Review


def fixture(test):
    test.user = User.objects.create(phone="13900000001", nickname="原昵称")
    test.admin = User.objects.create(
        phone="13900000002", nickname="内容客服", is_staff=True, is_superuser=True
    )
    test.other = User.objects.create(phone="13900000003", nickname="其他人")
    test.client = APIClient()
    test.provider = make_provider_eligible(
        ProviderProfile.objects.create(user=test.user, status="approved")
    )
    test.category = ServiceCategory.objects.create(name="桌球", slug="moderation-test")
    test.service = ProviderService.objects.create(
        provider=test.provider,
        category=test.category,
        billing_type="hourly",
        price_amount=10000,
        description="公开服务说明",
    )


def decide(review, actor, action="approve"):
    return services.decide(
        review.pk, actor=actor, action=action, revision=review.revision, reason="已人工逐项核实内容"
    )


def activity(test):
    now = timezone.now()
    return Activity.objects.create(
        organizer=test.user,
        title="周末桌游",
        description="一起玩桌游",
        participation_rules="文明参与",
        status="recruiting",
        starts_at=now + timedelta(days=2),
        ends_at=now + timedelta(days=2, hours=2),
        formation_deadline=now + timedelta(days=1),
        meeting_place_name="桌游店",
        meeting_address="测试街道",
        city_code="130400",
        city_name="邯郸市",
        source_longitude="114.50",
        source_latitude="36.60",
        meeting_point=Point(114.5, 36.6, srid=4326),
        capacity=6,
        min_participants=2,
        aa_principal_amount=1000,
        refund_template_version="standard-v1",
        refund_rule_snapshot={},
    )


@override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=False)
class ContentWorkflowTests(TestCase):
    def setUp(self):
        fixture(self)

    def test_nickname_is_staged_and_only_published_after_review(self):
        self.client.force_authenticate(self.user)
        response = self.client.patch("/api/v1/users/me/", {"nickname": "新昵称"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["nickname"], "原昵称")
        self.assertEqual(response.data["data"]["content_reviews"][0]["status"], "manual")
        review = Review.objects.get(kind="nickname")
        decide(review, self.admin)
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "新昵称")
        self.assertEqual(review.events.count(), 2)

    def test_identical_submission_is_idempotent_and_replacement_invalidates_old(self):
        first = services.submit("nickname", self.user, candidate="第一次昵称")
        self.assertEqual(
            services.submit("nickname", self.user, candidate="第一次昵称").pk, first.pk
        )
        second = services.submit("nickname", self.user, candidate="第二次昵称")
        first.refresh_from_db()
        self.assertFalse(first.is_current)
        with self.assertRaises(ValidationError):
            decide(first, self.admin)
        decide(second, self.admin)
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "第二次昵称")

    def test_stale_revision_and_short_reason_cannot_change_content(self):
        review = services.submit("nickname", self.user, candidate="新昵称")
        for revision, reason in [(99, "人工逐项核查"), (1, "好")]:
            with self.assertRaises(ValidationError):
                services.decide(
                    review.pk, actor=self.admin, action="approve", revision=revision, reason=reason
                )
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "原昵称")

    def test_avatar_upload_does_not_replace_or_delete_current_avatar(self):
        old = MediaAsset.objects.create(
            owner=self.user,
            scope="public",
            category="avatar",
            status="uploaded",
            object_key="public/old.jpg",
        )
        new = MediaAsset.objects.create(
            owner=self.user,
            scope="public",
            category="avatar",
            status="uploaded",
            object_key="public/new.jpg",
        )
        self.user.avatar_object_key = old.object_key
        self.user.save()
        self.client.force_authenticate(self.user)
        with (
            patch("mediafiles.views.AvatarUploadView.create_asset", return_value=new),
            patch("mediafiles.views.delete_public_object") as delete,
        ):
            response = self.client.post("/api/v1/media/avatars/", {}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        delete.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar_object_key, old.object_key)
        decide(Review.objects.get(kind="avatar"), self.admin)
        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar_object_key, new.object_key)

    def test_private_or_foreign_avatar_is_never_submitted(self):
        for owner, scope in [(self.other, "public"), (self.user, "private")]:
            asset = MediaAsset.objects.create(
                owner=owner,
                scope=scope,
                category="avatar",
                status="uploaded",
                object_key=f"{scope}/{owner.pk}.jpg",
            )
            with self.assertRaises(ValidationError):
                services.submit("avatar", self.user, candidate=asset.pk)
        self.assertEqual(Review.objects.count(), 0)

    def test_changed_asset_cannot_be_manually_published(self):
        asset = MediaAsset.objects.create(
            owner=self.user,
            scope="public",
            category="avatar",
            status="uploaded",
            object_key="public/pinned.jpg",
            etag="one",
        )
        review = services.submit("avatar", self.user, candidate=asset.pk)
        asset.etag = "two"
        asset.save()
        with self.assertRaises(ValidationError):
            decide(review, self.admin)

    def test_provider_takedown_blocks_public_listing_without_changing_business_status(self):
        from providers.selectors import public_providers

        self.assertTrue(public_providers().filter(pk=self.provider.pk).exists())
        review = decide(services.submit("provider", self.provider), self.admin)
        review = decide(review, self.admin, "withdraw")
        self.provider.refresh_from_db()
        self.assertTrue(self.provider.content_hidden)
        self.assertEqual(self.provider.status, "approved")
        self.assertFalse(public_providers().filter(pk=self.provider.pk).exists())
        decide(review, self.admin, "restore")
        self.assertTrue(public_providers().filter(pk=self.provider.pk).exists())

    def test_service_takedown_blocks_booking_even_with_known_id(self):
        from orders.services import validate_booking

        review = decide(services.submit("service", self.service), self.admin)
        decide(review, self.admin, "withdraw")
        self.service.refresh_from_db()
        self.assertTrue(self.service.is_active)
        self.assertTrue(self.service.content_hidden)
        with self.assertRaises(ValidationError):
            validate_booking(self.service, timezone.now() + timedelta(days=1), 120)
        from providers.selectors import public_providers

        self.assertFalse(public_providers().filter(pk=self.provider.pk).exists())

    def test_activity_takedown_hides_from_discovery_and_blocks_new_join_but_preserves_fulfillment(
        self,
    ):
        from activities.selectors import upcoming_public_activities
        from activities.services import _validate_participation_eligibility

        obj = activity(self)
        participant = ActivityParticipation.objects.create(
            activity=obj,
            user=self.other,
            status="active",
            aa_principal_amount=1000,
            platform_service_fee_amount=100,
            payable_amount=1100,
            rule_confirmed_at=timezone.now(),
            pricing_snapshot={},
            refund_rule_snapshot={},
        )
        review = decide(services.submit("activity", obj), self.admin)
        decide(review, self.admin, "withdraw")
        obj.refresh_from_db()
        self.assertEqual(obj.status, "recruiting")
        self.assertEqual(ActivityParticipation.objects.get(pk=participant.pk).status, "active")
        self.assertFalse(upcoming_public_activities().filter(pk=obj.pk).exists())
        with self.assertRaises(ValidationError):
            _validate_participation_eligibility(obj, self.admin, timezone.now())
        self.assertEqual(self.client.get(f"/api/v1/activities/{obj.pk}/").status_code, 404)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/v1/activities/{obj.pk}/").status_code, 200)

    def test_business_approval_requires_current_clearance(self):
        with self.assertRaises(ValidationError):
            services.ensure_approved("service", self.service)
        review = decide(services.submit("service", self.service), self.admin)
        services.ensure_approved("service", self.service)
        self.service.description = "已修改服务说明"
        self.service.save()
        with self.assertRaises(ValidationError):
            services.ensure_approved("service", self.service)
        with self.assertRaises(ValidationError):
            decide(review, self.admin, "withdraw")

    def test_admin_permissions_and_fenced_api_actions(self):
        review = services.submit("nickname", self.user, candidate="新昵称")
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/v1/admin/content-reviews/").status_code, 403)
        self.client.force_authenticate(self.admin)
        listing = self.client.get("/api/v1/admin/content-reviews/?page=1&kind=nickname&q=新昵称")
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(listing.data["data"]["pagination"]["total"], 1)
        response = self.client.post(
            f"/api/v1/admin/content-reviews/{review.pk}/",
            {"action": "approve", "revision": 99, "reason": "人工检查内容正常"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        detail = self.client.get(f"/api/v1/admin/content-reviews/{review.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("snapshot", detail.data["data"])

    def test_real_time_work_queue_contains_manual_content(self):
        from backoffice.access import resolve_admin_access
        from backoffice.operations_queue import queues

        review = services.submit("nickname", self.user, candidate="待核查昵称")
        queue = next(
            q for q in queues(resolve_admin_access(self.admin)) if q.key == "content_review"
        )
        self.assertEqual(list(queue.queryset.values_list("pk", flat=True)), [review.pk])
        self.client.force_authenticate(self.admin)
        summary = self.client.get("/api/v1/admin/work/summary/")
        self.assertEqual(summary.status_code, 200, summary.data)
        rows = self.client.get("/api/v1/admin/work/items/?queue=content_review")
        self.assertEqual(rows.status_code, 200, rows.data)

    @override_settings(CONTENT_REVIEW_DAILY_LIMIT=1)
    def test_daily_budget_and_disabled_rollout(self):
        services.submit("nickname", self.user, candidate="第一次")
        with self.assertRaises(ValidationError):
            services.submit("nickname", self.user, candidate="第二次")
        with override_settings(CONTENT_REVIEW_ENABLED=False):
            self.assertIsNone(services.submit("nickname", self.user, candidate="第三次"))
            services.ensure_approved("provider", self.provider)


@override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=True)
class ContentWorkerTests(TransactionTestCase):
    def setUp(self):
        fixture(self)

    def test_text_pass_is_published_and_not_processed_twice(self):
        review = services.submit("nickname", self.user, candidate="新昵称")
        with patch(
            "moderation.gateway.detect_text",
            return_value={
                "suggestion": "Pass",
                "request_id": "request-1",
                "label": "Normal",
                "score": 0,
            },
        ) as detect:
            self.assertTrue(services.process_one(review.pk))
            self.assertFalse(services.process_one(review.pk))
        self.assertEqual(detect.call_count, 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "新昵称")

    def test_machine_review_block_and_timeout_never_publish_nickname(self):
        for suggestion in ("Review", "Block", "timeout"):
            review = services.submit("nickname", self.user, candidate=f"新昵称{suggestion}")
            kwargs = (
                {"side_effect": gateway.DetectionUnavailable()}
                if suggestion == "timeout"
                else {
                    "return_value": {
                        "suggestion": suggestion,
                        "request_id": "request-1",
                        "label": "",
                        "score": None,
                    }
                }
            )
            with patch("moderation.gateway.detect_text", **kwargs):
                services.process_one(review.pk)
            review.refresh_from_db()
            self.assertEqual(review.status, "rejected" if suggestion == "Block" else "manual")
            self.user.refresh_from_db()
            self.assertEqual(self.user.nickname, "原昵称")

    def test_late_machine_result_cannot_overwrite_human_decision(self):
        review = services.submit("nickname", self.user, candidate="新昵称")

        def late(*args):
            fresh = Review.objects.get(pk=review.pk)
            decide(fresh, self.admin, "reject")
            return {"suggestion": "Pass", "request_id": "late", "label": "", "score": None}

        with patch("moderation.gateway.detect_text", side_effect=late):
            services.process_one(review.pk)
        review.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(review.status, "rejected")
        self.assertEqual(self.user.nickname, "原昵称")

    def test_newer_submission_fences_old_worker(self):
        first = services.submit("nickname", self.user, candidate="第一次昵称")

        def late(*args):
            services.submit("nickname", self.user, candidate="第二次昵称")
            return {"suggestion": "Pass", "request_id": "late", "label": "", "score": None}

        with patch("moderation.gateway.detect_text", side_effect=late):
            services.process_one(first.pk)
        first.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(first.status, "superseded")
        self.assertEqual(self.user.nickname, "原昵称")

    def test_unexpired_lease_cannot_be_claimed_twice(self):
        review = services.submit("nickname", self.user, candidate="新昵称")
        self.assertIsNotNone(services._claim(review.pk))
        self.assertIsNone(services._claim(review.pk))
        self.assertNotIn(review.pk, services.pending_ids())

    def test_video_task_is_persisted_and_polled_without_resubmission(self):
        video = MediaAsset.objects.create(
            owner=self.user,
            scope="public",
            category="provider_video",
            status="uploaded",
            object_key="public/test.mp4",
        )
        self.provider.gallery_items.all().delete()
        self.provider.gallery_items.create(asset=video, position=0)
        review = services.submit("provider", self.provider)
        passed = {"suggestion": "Pass", "request_id": "r1", "label": "Normal", "score": 0}
        with (
            patch("moderation.gateway.detect_text", return_value=passed),
            patch(
                "moderation.gateway.create_video",
                return_value={"task_id": "task-1", "request_id": "r2"},
            ) as create,
            patch("moderation.gateway.query_video", return_value=passed) as query,
        ):
            services.process_one(review.pk)
            self.assertEqual(review.items.get(kind="video").task_id, "task-1")
            query.assert_not_called()
            Review.objects.filter(pk=review.pk).update(next_attempt_at=None)
            services.process_one(review.pk)
            self.assertEqual(create.call_count, 1)
            query.assert_called_once()
        review.refresh_from_db()
        self.assertEqual(review.status, "approved")

    def test_uncertain_video_submission_is_not_automatically_resubmitted(self):
        video = MediaAsset.objects.create(
            owner=self.user,
            scope="public",
            category="provider_video",
            status="uploaded",
            object_key="public/unknown.mp4",
        )
        self.provider.gallery_items.create(asset=video, position=0)
        review = services.submit("provider", self.provider)
        with (
            patch("moderation.gateway.detect_text", side_effect=gateway.DetectionUnavailable()),
            patch(
                "moderation.gateway.create_video", side_effect=gateway.DetectionUnavailable()
            ) as create,
        ):
            services.process_one(review.pk)
            review.refresh_from_db()
            review = decide(review, self.admin, "retry")
            services.process_one(review.pk)
            self.assertEqual(create.call_count, 1)
        review.refresh_from_db()
        self.assertEqual(review.status, "manual")

    def test_changed_media_is_not_sent_to_vendor(self):
        review = services.submit("provider", self.provider)
        MediaAsset.objects.filter(pk=self.provider.lifestyle_photo_id).update(scope="private")
        with (
            patch("moderation.gateway.detect_text", side_effect=gateway.DetectionUnavailable()),
            patch("moderation.gateway.detect_image") as image,
        ):
            services.process_one(review.pk)
        image.assert_not_called()
        review.refresh_from_db()
        self.assertEqual(review.status, "manual")


@override_settings(
    TENCENT_CONTENT_REVIEW_ENABLED=True,
    TENCENT_CONTENT_REVIEW_TEXT_BIZ_TYPE="",
    TENCENT_CONTENT_REVIEW_IMAGE_BIZ_TYPE="",
    TENCENT_CONTENT_REVIEW_VIDEO_BIZ_TYPE="test_video",
)
class TencentContractTests(SimpleTestCase):
    def test_text_contract_uses_base64_and_validates_echoed_identity(self):
        response = {
            "Suggestion": "Pass",
            "DataId": "content-1",
            "RequestId": "request-1",
            "Label": "Normal",
            "Score": 0,
        }
        with patch("moderation.gateway._call", return_value=response) as call:
            self.assertEqual(gateway.detect_text("公开昵称", "content-1")["suggestion"], "Pass")
        product, action, payload = call.call_args.args
        self.assertEqual((product, action), ("tms", "TextModeration"))
        self.assertEqual(base64.b64decode(payload["Content"]).decode(), "公开昵称")
        with patch("moderation.gateway._call", return_value={**response, "DataId": "wrong"}):
            with self.assertRaises(gateway.DetectionUnavailable):
                gateway.detect_text("文字", "content-1")

    def test_video_requires_real_final_status_and_echoed_task(self):
        for status in ("RUNNING", "PENDING", "ERROR", "FINISH"):
            response = {
                "Status": status,
                "TaskId": "task-1",
                "DataId": "content-1",
                "RequestId": "req-1",
                "Suggestion": "Pass",
            }
            with patch("moderation.gateway._call", return_value=response):
                if status in ("RUNNING", "PENDING"):
                    self.assertIsNone(gateway.query_video("task-1", "content-1"))
                elif status == "ERROR":
                    with self.assertRaises(gateway.DetectionUnavailable):
                        gateway.query_video("task-1", "content-1")
                else:
                    self.assertEqual(
                        gateway.query_video("task-1", "content-1")["suggestion"], "Pass"
                    )

    def test_unknown_suggestion_is_never_a_pass(self):
        with self.assertRaises(gateway.DetectionUnavailable):
            gateway._decision({"Suggestion": "Unknown", "RequestId": "req-1"})

    @override_settings(
        TENCENT_CONTENT_REVIEW_SECRET_ID="offline-only-id",
        TENCENT_CONTENT_REVIEW_SECRET_KEY="offline-only-secret",
    )
    def test_official_sdk_request_model_and_version(self):
        from tencentcloud.tms.v20201229.models import TextModerationResponse

        response = TextModerationResponse()
        response.from_json_string('{"Suggestion":"Pass","RequestId":"req-1","DataId":"content-1"}')
        with patch(
            "tencentcloud.tms.v20201229.tms_client.TmsClient.TextModeration", return_value=response
        ) as call:
            gateway.detect_text("公开内容", "content-1")
        self.assertEqual(call.call_args.args[0].DataId, "content-1")
