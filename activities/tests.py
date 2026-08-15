from datetime import timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import User

from .models import Activity, ActivityCategory


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
