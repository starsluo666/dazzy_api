from datetime import timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from mediafiles.models import MediaAsset

from .models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityCategory,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivitySettlement,
)
from .services import calculate_publish_service_fee, process_activity_timeouts


class ActivityModelTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(phone="13800000002", password="test-password")
        self.category = ActivityCategory.objects.create(name="台球", slug="billiards")
        self.cover = MediaAsset.objects.create(
            owner=self.organizer,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.ACTIVITY_COVER,
            status=MediaAsset.Status.UPLOADED,
            object_key="dazzy-test/public/activity-covers/test.webp",
        )

    def build_activity(self, **overrides):
        starts_at = timezone.now() + timedelta(days=3)
        values = {
            "organizer": self.organizer,
            "category": self.category,
            "title": "周末台球局",
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(hours=3),
            "formation_deadline": starts_at - timedelta(hours=12),
            "meeting_place_name": "测试台球俱乐部",
            "meeting_address": "北京市测试地址",
            "source_longitude": Decimal("116.4039810"),
            "source_latitude": Decimal("39.9150010"),
            "meeting_point": Point(116.397755, 39.913873, srid=4326),
            "capacity": 6,
            "min_participants": 4,
            "description": "一起打球",
            "participation_rules": "准时到场",
            "aa_principal_amount": 4800,
            "refund_template_version": "standard-v1",
            "cover_id": str(self.cover.pk),
            "refund_rule_snapshot": {"version": "standard-v1"},
        }
        values.update(overrides)
        return Activity(**values)

    def test_activity_accepts_valid_invariants(self):
        activity = self.build_activity()
        activity.full_clean()
        activity.save()

        self.assertEqual(activity.aa_principal_amount, 4800)
        self.assertEqual(activity.meeting_point.srid, 4326)

    def test_publish_service_fee_uses_half_up_rounding(self):
        self.assertEqual(calculate_publish_service_fee(6805), 681)

    def test_minimum_participants_cannot_exceed_capacity(self):
        activity = self.build_activity(capacity=4, min_participants=5)

        with self.assertRaises(ValidationError):
            activity.full_clean()

    def test_activity_list_filters_public_upcoming_activities(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.full_clean()
        activity.save()

        response = self.client.get(
            "/api/v1/activities/",
            {
                "longitude": "116.4039810",
                "latitude": "39.9150010",
                "ordering": "distance",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
        item = response.json()["data"]["items"][0]
        self.assertEqual(item["title"], "周末台球局")
        self.assertNotIn("meeting_point", item)
        self.assertNotIn("meeting_address", item)

    def test_activity_detail_returns_display_fields_and_calculated_fee(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.full_clean()
        activity.save()

        response = self.client.get(f"/api/v1/activities/{activity.pk}/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["meeting_address"], "北京市测试地址")
        self.assertEqual(data["participant_count"], 0)
        self.assertEqual(data["platform_service_fee_amount"], 480)
        self.assertEqual(data["payable_amount"], 5280)

    def test_activity_detail_returns_not_found_for_unknown_id(self):
        response = self.client.get("/api/v1/activities/999999/")

        self.assertEqual(response.status_code, 404)

    def test_activity_draft_detail_is_only_visible_to_organizer(self):
        activity = self.build_activity(status=Activity.Status.DRAFT)
        activity.save()

        anonymous = self.client.get(f"/api/v1/activities/{activity.pk}/")
        self.client.force_login(self.organizer)
        organizer = self.client.get(f"/api/v1/activities/{activity.pk}/")

        self.assertEqual(anonymous.status_code, 404)
        self.assertEqual(organizer.status_code, 200)

    @override_settings(DEBUG=True)
    def test_participation_join_is_idempotent_and_visible_in_detail(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000009", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)

        first = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/",
            {"channel": "mock_wechat"},
            content_type="application/json",
        )
        second = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        paid = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        detail = self.client.get(f"/api/v1/activities/{activity.pk}/")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(paid.status_code, 200, paid.json())
        self.assertEqual(ActivityParticipation.objects.count(), 1)
        self.assertEqual(detail.json()["data"]["participant_count"], 1)
        self.assertTrue(detail.json()["data"]["is_joined"])
        self.assertEqual(detail.json()["data"]["participation_status"], "active")

    @override_settings(DEBUG=True)
    def test_participation_forms_activity_and_cancel_reopens_it(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            min_participants=2,
            capacity=3,
        )
        activity.save()
        first = User.objects.create_user(
            phone="13800000010", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        second = User.objects.create_user(
            phone="13800000011", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )

        self.client.force_login(first)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        self.client.force_login(second)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        joined = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        activity.refresh_from_db()

        self.assertEqual(joined.status_code, 200, joined.json())
        self.assertEqual(activity.status, Activity.Status.FORMED)
        self.assertEqual(joined.json()["data"]["participant_count"], 2)

        cancelled = self.client.delete(f"/api/v1/activities/{activity.pk}/participation/")
        activity.refresh_from_db()

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(activity.status, Activity.Status.RECRUITING)
        self.assertEqual(
            ActivityParticipation.objects.filter(status="active").count(),
            1,
        )

    @override_settings(DEBUG=True)
    def test_participation_rejects_organizer_and_full_activity(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            min_participants=2,
            capacity=2,
        )
        activity.save()
        self.client.force_login(self.organizer)
        organizer_response = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/"
        )
        self.assertEqual(organizer_response.status_code, 403)

        for suffix in (12, 13):
            user = User.objects.create_user(
                phone=f"138000000{suffix}", password="test",
                verification_status=User.VerificationStatus.VERIFIED,
            )
            self.client.force_login(user)
            self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
            self.client.post(
                f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
            )

        extra = User.objects.create_user(
            phone="13800000014", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(extra)
        full_response = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/"
        )

        self.assertEqual(full_response.status_code, 400)
        self.assertIn("名额已满", str(full_response.json()))

    def test_my_activities_lists_joined_and_cancelled_records(self):
        active_activity = self.build_activity(
            title="正在参与的活动", status=Activity.Status.RECRUITING
        )
        active_activity.save()
        cancelled_activity = self.build_activity(
            title="已经取消的活动", status=Activity.Status.RECRUITING
        )
        cancelled_activity.save()
        participant = User.objects.create_user(phone="13800000015", password="test")
        ActivityParticipation.objects.create(
            activity=active_activity,
            user=participant,
            status=ActivityParticipation.Status.ACTIVE,
            joined_at=timezone.now(),
        )
        ActivityParticipation.objects.create(
            activity=cancelled_activity,
            user=participant,
            status=ActivityParticipation.Status.CANCELLED,
            cancelled_at=timezone.now(),
        )
        self.client.force_login(participant)

        upcoming = self.client.get("/api/v1/activities/mine/", {"state": "upcoming"})
        history = self.client.get("/api/v1/activities/mine/", {"state": "history"})

        self.assertEqual(upcoming.status_code, 200)
        self.assertEqual(upcoming.json()["data"]["pagination"]["total"], 1)
        self.assertEqual(
            upcoming.json()["data"]["items"][0]["participation_status"], "active"
        )
        self.assertEqual(history.json()["data"]["pagination"]["total"], 1)
        self.assertEqual(
            history.json()["data"]["items"][0]["participation_status"], "cancelled"
        )

    def test_my_activities_lists_organized_activities(self):
        activity = self.build_activity(
            title="我发起的活动", status=Activity.Status.RECRUITING
        )
        activity.save()
        self.client.force_login(self.organizer)

        response = self.client.get(
            "/api/v1/activities/mine/", {"role": "organized"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
        item = response.json()["data"]["items"][0]
        self.assertEqual(item["title"], "我发起的活动")
        self.assertIsNone(item["participation_status"])

    @override_settings(DEBUG=True)
    def test_participation_payment_locks_seat_and_timeout_releases_it(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING, capacity=2, min_participants=2
        )
        activity.save()
        first = User.objects.create_user(
            phone="13800000031", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        second = User.objects.create_user(
            phone="13800000032", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        third = User.objects.create_user(
            phone="13800000033", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        for user in (first, second):
            self.client.force_login(user)
            response = self.client.post(
                f"/api/v1/activities/{activity.pk}/participation/"
            )
            self.assertEqual(response.status_code, 201)
        self.client.force_login(third)
        full = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.assertEqual(full.status_code, 400)

        first_participation = ActivityParticipation.objects.get(
            activity=activity, user=first
        )
        first_participation.payment_orders.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        first_participation.payment_expires_at = timezone.now() - timedelta(seconds=1)
        first_participation.save(update_fields=("payment_expires_at",))
        retried = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")

        self.assertEqual(retried.status_code, 201)
        first_participation.refresh_from_db()
        self.assertEqual(first_participation.status, ActivityParticipation.Status.EXPIRED)
        self.assertEqual(
            first_participation.payment_orders.get().status,
            ActivityParticipationPaymentOrder.Status.CLOSED,
        )

    @override_settings(DEBUG=True)
    def test_participant_cancellation_creates_rule_based_refund_idempotently(self):
        starts_at = timezone.now() + timedelta(hours=4)
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            formation_deadline=timezone.now() + timedelta(hours=1),
        )
        activity.save()
        participant = User.objects.create_user(
            phone="13800000034", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )

        first = self.client.delete(
            f"/api/v1/activities/{activity.pk}/participation/"
        )
        second = self.client.delete(
            f"/api/v1/activities/{activity.pk}/participation/"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ActivityParticipationRefundOrder.objects.count(), 1)
        refund = ActivityParticipationRefundOrder.objects.get()
        self.assertEqual(refund.principal_refund_amount, 3360)
        self.assertEqual(refund.service_fee_refund_amount, 0)
        self.assertEqual(refund.retained_principal_amount, 1440)
        self.assertEqual(
            refund.retained_principal_destination,
            ActivityParticipationRefundOrder.PrincipalDestination.ORGANIZER,
        )

    @override_settings(DEBUG=True)
    def test_activity_after_sales_is_idempotent_and_links_paid_participation(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000035", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        payload = {
            "reason": "illness_or_accident",
            "description": "突发身体不适，申请平台协助退款。",
        }

        first = self.client.post(
            f"/api/v1/activities/{activity.pk}/after-sales/",
            payload,
            content_type="application/json",
        )
        second = self.client.post(
            f"/api/v1/activities/{activity.pk}/after-sales/",
            payload,
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["case_no"], second.json()["data"]["case_no"])
        case = ActivityAfterSalesCase.objects.get()
        self.assertEqual(case.requested_amount, 5280)
        self.assertEqual(case.status, ActivityAfterSalesCase.Status.PENDING)
        cancellation = self.client.delete(
            f"/api/v1/activities/{activity.pk}/participation/"
        )
        self.assertEqual(cancellation.status_code, 400)
        self.assertEqual(ActivityParticipationRefundOrder.objects.count(), 0)

    def test_timeout_processor_fails_unformed_activity_and_refunds_publish_order(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            formation_deadline=timezone.now() - timedelta(minutes=1),
        )
        activity.save()
        ActivityPublishOrder.objects.create(
            order_no="FAILED-TO-FORM-PUBLISH",
            activity=activity,
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityPublishOrder.Status.PAID,
            paid_at=timezone.now() - timedelta(days=1),
        )

        result = process_activity_timeouts()

        activity.refresh_from_db()
        self.assertEqual(result["processed_activity_count"], 1)
        self.assertEqual(activity.status, Activity.Status.FAILED_TO_FORM)
        self.assertEqual(activity.publish_orders.get().status, ActivityPublishOrder.Status.REFUNDED)
        self.assertEqual(
            activity.refund_records.get().refund_type,
            "failed_to_form",
        )

    def test_activity_lifecycle_creates_and_advances_settlement_idempotently(self):
        now = timezone.now()
        activity = self.build_activity(
            status=Activity.Status.FORMED,
            starts_at=now - timedelta(hours=3),
            ends_at=now - timedelta(hours=1),
            formation_deadline=now - timedelta(hours=4),
            min_participants=2,
        )
        activity.save()
        ActivityPublishOrder.objects.create(
            order_no="SETTLEMENT-PUBLISH",
            activity=activity,
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityPublishOrder.Status.PAID,
            paid_at=now - timedelta(days=1),
        )
        participant = User.objects.create_user(phone="13800000038", password="test")
        participation = ActivityParticipation.objects.create(
            activity=activity,
            user=participant,
            status=ActivityParticipation.Status.ACTIVE,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            joined_at=now - timedelta(days=1),
        )
        ActivityParticipationPaymentOrder.objects.create(
            participation=participation,
            payer=participant,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityParticipationPaymentOrder.Status.PAID,
            expires_at=now - timedelta(hours=20),
            paid_at=now - timedelta(hours=21),
        )

        first = process_activity_timeouts(now=now)
        activity.refresh_from_db()
        settlement = activity.settlement

        self.assertEqual(first["started_activity_count"], 1)
        self.assertEqual(first["completed_activity_count"], 1)
        self.assertEqual(first["settlement_created_count"], 1)
        self.assertEqual(activity.status, Activity.Status.COMPLETED)
        self.assertEqual(settlement.status, ActivitySettlement.Status.CONFIRMING)
        self.assertEqual(settlement.settlement_amount, 9600)
        self.assertEqual(settlement.platform_service_fee_amount, 960)

        process_activity_timeouts(now=settlement.confirmation_deadline)
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, ActivitySettlement.Status.RISK_FROZEN)

        process_activity_timeouts(now=settlement.freeze_until)
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, ActivitySettlement.Status.SETTLED)
        self.assertEqual(ActivitySettlement.objects.filter(activity=activity).count(), 1)

        self.client.force_login(self.organizer)
        organizer_detail = self.client.get(f"/api/v1/activities/{activity.pk}/")
        self.client.force_login(participant)
        participant_detail = self.client.get(f"/api/v1/activities/{activity.pk}/")
        self.assertEqual(
            organizer_detail.json()["data"]["settlement"]["settlement_amount"], 9600
        )
        self.assertIsNone(
            participant_detail.json()["data"]["settlement"]["settlement_amount"]
        )

    @override_settings(DEBUG=True)
    def test_organizer_cancellation_refunds_participants_and_applies_responsibility(self):
        starts_at = timezone.now() + timedelta(hours=4)
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            formation_deadline=timezone.now() + timedelta(hours=1),
            published_at=timezone.now() - timedelta(days=1),
        )
        activity.save()
        ActivityPublishOrder.objects.create(
            order_no="ORGANIZER-CANCEL-ORDER",
            activity=activity,
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityPublishOrder.Status.PAID,
            paid_at=timezone.now() - timedelta(days=1),
        )
        participant = User.objects.create_user(
            phone="13800000036", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        self.client.force_login(self.organizer)

        response = self.client.post(
            f"/api/v1/activities/{activity.pk}/cancel/",
            {"reason": "场地临时无法使用"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        activity.refresh_from_db()
        self.assertEqual(activity.status, Activity.Status.CANCELLED)
        participant_refund = ActivityParticipationRefundOrder.objects.get()
        self.assertEqual(participant_refund.refund_amount, 5280)
        publish_refund = activity.refund_records.get()
        self.assertEqual(publish_refund.principal_amount, 3360)
        self.assertEqual(publish_refund.service_fee_amount, 0)
        self.assertEqual(publish_refund.retained_principal_destination, "platform")

    def activity_create_payload(self, **overrides):
        starts_at = timezone.now() + timedelta(days=3)
        payload = {
            "category_slug": self.category.slug,
            "title": "我发布的桌游活动",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=3)).isoformat(),
            "formation_deadline": (starts_at - timedelta(hours=12)).isoformat(),
            "meeting_place_name": "美乐城桌游空间",
            "meeting_address": "邯郸市丛台区人民路",
            "longitude": "114.5389610",
            "latitude": "36.6256570",
            "capacity": 8,
            "min_participants": 4,
            "description": "轻松认识新朋友",
            "participation_rules": "准时到场，文明参与",
            "aa_principal_amount": 6800,
            "refund_template_version": "standard-v1",
            "cover_id": str(self.cover.pk),
        }
        payload.update(overrides)
        return payload

    @override_settings(DEBUG=True)
    def test_verified_user_can_create_paid_activity_draft(self):
        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        self.client.force_login(self.organizer)

        response = self.client.post(
            "/api/v1/activities/", self.activity_create_payload(), content_type="application/json"
        )

        self.assertEqual(response.status_code, 201)
        activity = Activity.objects.get(title="我发布的桌游活动")
        self.assertEqual(activity.status, Activity.Status.DRAFT)
        self.assertEqual(activity.refund_template_version, "standard-v1")
        self.assertEqual(response.json()["data"]["next_step"], "payment")

        payment_order = self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/"
        )
        first_order_no = payment_order.json()["data"]["order_no"]
        ActivityPublishOrder.objects.filter(order_no=first_order_no).update(
            status=ActivityPublishOrder.Status.CANCELLED
        )
        retry_order = self.client.post(f"/api/v1/activities/{activity.pk}/publish-order/")
        paid = self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/simulate-payment/"
        )
        activity.refresh_from_db()
        order = ActivityPublishOrder.objects.get(
            activity=activity, status=ActivityPublishOrder.Status.PAID
        )

        self.assertEqual(payment_order.status_code, 201)
        self.assertEqual(payment_order.json()["data"]["payable_amount"], 7480)
        self.assertEqual(retry_order.status_code, 201)
        self.assertNotEqual(retry_order.json()["data"]["order_no"], first_order_no)
        self.assertEqual(ActivityPublishOrder.objects.filter(activity=activity).count(), 2)
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(order.status, ActivityPublishOrder.Status.PAID)
        self.assertEqual(activity.status, Activity.Status.PENDING_REVIEW)

    def test_activity_create_enforces_verification_and_start_window(self):
        self.client.force_login(self.organizer)
        unverified = self.client.post(
            "/api/v1/activities/", self.activity_create_payload(), content_type="application/json"
        )
        self.assertEqual(unverified.status_code, 403)

        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        too_soon = timezone.now() + timedelta(hours=24)
        invalid = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(
                starts_at=too_soon.isoformat(),
                ends_at=(too_soon + timedelta(hours=2)).isoformat(),
                formation_deadline=(too_soon - timedelta(hours=2)).isoformat(),
            ),
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("48小时", str(invalid.json()))

    def test_activity_create_honors_category_city_people_and_amount_rules(self):
        self.category.city_codes = ["130400"]
        self.category.min_capacity = 4
        self.category.max_capacity = 8
        self.category.min_aa_principal_amount = 5000
        self.category.max_aa_principal_amount = 8000
        self.category.save()
        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        self.client.force_login(self.organizer)

        wrong_city = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(city_code="110100", city_name="北京市"),
            content_type="application/json",
        )
        too_many = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(
                city_code="130400", city_name="邯郸市", capacity=9
            ),
            content_type="application/json",
        )
        too_expensive = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(
                city_code="130400", city_name="邯郸市", aa_principal_amount=8100
            ),
            content_type="application/json",
        )

        self.assertEqual(wrong_city.status_code, 400)
        self.assertEqual(too_many.status_code, 400)
        self.assertEqual(too_expensive.status_code, 400)

    def test_activity_report_is_idempotent_and_private_draft_is_not_reportable(self):
        public_activity = self.build_activity(status=Activity.Status.RECRUITING)
        public_activity.save()
        draft = self.build_activity(title="私有草稿", status=Activity.Status.DRAFT)
        draft.save()
        reporter = User.objects.create_user(phone="13800000088", password="test")
        self.client.force_login(reporter)

        first = self.client.post(
            f"/api/v1/activities/{public_activity.pk}/reports/",
            {"reason": "false_information", "description": "信息与现场不一致"},
            content_type="application/json",
        )
        second = self.client.post(
            f"/api/v1/activities/{public_activity.pk}/reports/",
            {"reason": "other"},
            content_type="application/json",
        )
        private = self.client.post(
            f"/api/v1/activities/{draft.pk}/reports/",
            {"reason": "other"},
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["case_no"], second.json()["data"]["case_no"])
        self.assertEqual(private.status_code, 404)
