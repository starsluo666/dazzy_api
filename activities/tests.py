from datetime import timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import User

from .models import Activity, ActivityCategory, ActivityParticipation


class ActivityModelTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(phone="13800000002", password="test-password")
        self.category = ActivityCategory.objects.create(name="台球", slug="billiards")

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

    def test_participation_join_is_idempotent_and_visible_in_detail(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(phone="13800000009", password="test")
        self.client.force_login(participant)

        first = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        second = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        detail = self.client.get(f"/api/v1/activities/{activity.pk}/")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ActivityParticipation.objects.count(), 1)
        self.assertEqual(detail.json()["data"]["participant_count"], 1)
        self.assertTrue(detail.json()["data"]["is_joined"])
        self.assertEqual(detail.json()["data"]["participation_status"], "active")

    def test_participation_forms_activity_and_cancel_reopens_it(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            min_participants=2,
            capacity=3,
        )
        activity.save()
        first = User.objects.create_user(phone="13800000010", password="test")
        second = User.objects.create_user(phone="13800000011", password="test")

        self.client.force_login(first)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.force_login(second)
        joined = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        activity.refresh_from_db()

        self.assertEqual(joined.status_code, 201)
        self.assertEqual(activity.status, Activity.Status.FORMED)
        self.assertEqual(joined.json()["data"]["participant_count"], 2)

        cancelled = self.client.delete(f"/api/v1/activities/{activity.pk}/participation/")
        activity.refresh_from_db()

        self.assertEqual(cancelled.status_code, 204)
        self.assertEqual(activity.status, Activity.Status.RECRUITING)
        self.assertEqual(
            ActivityParticipation.objects.filter(status="active").count(),
            1,
        )

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
            user = User.objects.create_user(phone=f"138000000{suffix}", password="test")
            self.client.force_login(user)
            self.client.post(f"/api/v1/activities/{activity.pk}/participation/")

        extra = User.objects.create_user(phone="13800000014", password="test")
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
        ActivityParticipation.objects.create(activity=active_activity, user=participant)
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
