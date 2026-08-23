from datetime import time, timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from mediafiles.models import MediaAsset

from .models import ProviderProfile, ProviderService, ProviderWeeklyAvailability, ServiceCategory


class ProviderModelTests(TestCase):
    def test_provider_service_uses_integer_minor_units_and_wgs84_point(self):
        user = User.objects.create_user(phone="13800000001", password="test-password")
        provider = ProviderProfile.objects.create(
            user=user,
            source_longitude=Decimal("116.4039810"),
            source_latitude=Decimal("39.9150010"),
            service_center=Point(116.397755, 39.913873, srid=4326),
        )
        category = ServiceCategory.objects.create(name="台球陪玩", slug="billiards")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=17800,
        )

        self.assertEqual(service.price_amount, 17800)
        self.assertEqual(provider.service_center.srid, 4326)
        self.assertEqual(provider.source_longitude, Decimal("116.4039810"))

    def test_provider_list_supports_gcj02_distance(self):
        user = User.objects.create_user(
            phone="13800000003", password="test-password", nickname="晓晓"
        )
        provider = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="110100",
            service_city_name="北京市",
            service_center=Point(116.397755, 39.913873, srid=4326),
        )
        category = ServiceCategory.objects.create(name="台球陪玩", slug="billiards-list")
        ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=17800,
        )

        response = self.client.get(
            "/api/v1/providers/",
            {
                "city_code": "110100",
                "longitude": "116.4039810",
                "latitude": "39.9150010",
                "ordering": "distance",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
        self.assertEqual(response.json()["data"]["items"][0]["nickname"], "晓晓")
        self.assertIsNotNone(response.json()["data"]["items"][0]["distance_km"])

    def test_provider_detail_returns_public_profile_and_active_services(self):
        user = User.objects.create_user(
            phone="13800000004",
            password="test-password",
            nickname="可可",
            birth_date="2000-05-23",
        )
        lifestyle_photo = MediaAsset.objects.create(
            owner=user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{user.public_id}/detail.webp",
        )
        provider = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.APPROVED,
            lifestyle_photo=lifestyle_photo,
            service_city_code="110100",
            service_city_name="北京市",
            bio="喜欢旅行与摄影",
        )
        category = ServiceCategory.objects.create(name="旅行陪伴", slug="travel-detail")
        ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=17800,
        )

        response = self.client.get(f"/api/v1/providers/{user.public_id}/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["nickname"], "可可")
        self.assertEqual(data["services"][0]["price_amount"], 17800)
        self.assertEqual(data["birth_date"], "2000-05-23")
        self.assertIn("detail.webp", data["lifestyle_photo_url"])

    def test_provider_detail_hides_unapproved_profile(self):
        user = User.objects.create_user(phone="13800000005", password="test-password")
        ProviderProfile.objects.create(user=user, status=ProviderProfile.Status.DRAFT)

        response = self.client.get(f"/api/v1/providers/{user.public_id}/")

        self.assertEqual(response.status_code, 404)

    def test_provider_list_hides_approved_profile_without_active_service(self):
        user = User.objects.create_user(phone="13800000009", password="test-password")
        ProviderProfile.objects.create(user=user, status=ProviderProfile.Status.APPROVED)

        response = self.client.get("/api/v1/providers/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 0)
        self.assertEqual(self.client.get(f"/api/v1/providers/{user.public_id}/").status_code, 404)


class ProviderSelfManagementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            phone="13800000021", password="test-password", nickname="申请人"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_user_can_save_and_submit_provider_application(self):
        self.user.gender = User.Gender.FEMALE
        self.user.save(update_fields=("gender",))
        lifestyle_photo = MediaAsset.objects.create(
            owner=self.user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{self.user.public_id}/lifestyle.webp",
        )
        draft = self.client.patch(
            "/api/v1/providers/me/application/",
            {
                "bio": "我熟悉本地路线，也喜欢摄影和旅行。",
                "lifestyle_photo_id": str(lifestyle_photo.id),
                "service_city_code": "110100",
                "service_city_name": "北京市",
                "max_service_radius_km": 15,
                "invitation_code": "DAZZY",
            },
            format="json",
        )
        self.assertEqual(draft.status_code, 200)
        self.assertEqual(draft.json()["data"]["status"], ProviderProfile.Status.DRAFT)
        self.assertEqual(draft.json()["data"]["gender"], User.Gender.FEMALE)
        self.assertEqual(draft.json()["data"]["lifestyle_photo_id"], str(lifestyle_photo.id))

        submitted = self.client.post(
            "/api/v1/providers/me/application/submit/",
            {"agreement_accepted": True},
            format="json",
        )
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.json()["data"]["status"], ProviderProfile.Status.PENDING)

        locked = self.client.patch(
            "/api/v1/providers/me/application/",
            {"bio": "提交后不可直接修改资料内容。"},
            format="json",
        )
        self.assertEqual(locked.status_code, 400)

    def test_provider_application_requires_lifestyle_photo_before_submission(self):
        self.client.patch(
            "/api/v1/providers/me/application/",
            {
                "bio": "我熟悉本地路线，也喜欢摄影和旅行。",
                "service_city_code": "130400",
                "service_city_name": "邯郸市",
            },
            format="json",
        )

        submitted = self.client.post(
            "/api/v1/providers/me/application/submit/",
            {"agreement_accepted": True},
            format="json",
        )

        self.assertEqual(submitted.status_code, 400)
        self.assertIn("生活照", str(submitted.json()))

    def test_provider_application_rejects_another_users_lifestyle_photo(self):
        other = User.objects.create_user(phone="13800000029", password="test-password")
        lifestyle_photo = MediaAsset.objects.create(
            owner=other,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{other.public_id}/lifestyle.webp",
        )

        response = self.client.patch(
            "/api/v1/providers/me/application/",
            {"lifestyle_photo_id": str(lifestyle_photo.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_provider_application_rejects_radius_outside_supported_range(self):
        too_small = self.client.patch(
            "/api/v1/providers/me/application/",
            {"max_service_radius_km": 9},
            format="json",
        )
        too_large = self.client.patch(
            "/api/v1/providers/me/application/",
            {"max_service_radius_km": 71},
            format="json",
        )

        self.assertEqual(too_small.status_code, 400)
        self.assertEqual(too_large.status_code, 400)

    def test_approved_provider_can_manage_own_services(self):
        ProviderProfile.objects.create(user=self.user, status=ProviderProfile.Status.APPROVED)
        category = ServiceCategory.objects.create(name="城市漫游", slug="city-walk-manage")
        created = self.client.post(
            "/api/v1/providers/me/services/",
            {
                "category_id": category.id,
                "billing_type": ProviderService.BillingType.HOURLY,
                "price_amount": 12800,
                "estimated_duration_minutes": 120,
                "description": "陪你探索城市街区",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        service_id = created.json()["data"]["id"]

        updated = self.client.patch(
            f"/api/v1/providers/me/services/{service_id}/",
            {"price_amount": 15800},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["data"]["price_amount"], 15800)

        disabled = self.client.delete(f"/api/v1/providers/me/services/{service_id}/")
        self.assertEqual(disabled.status_code, 204)
        self.assertFalse(ProviderService.objects.get(id=service_id).is_active)

    def test_unapproved_user_cannot_manage_services(self):
        ProviderProfile.objects.create(user=self.user, status=ProviderProfile.Status.PENDING)
        response = self.client.get("/api/v1/providers/me/services/")
        self.assertEqual(response.status_code, 403)

    def test_approved_provider_can_manage_schedule_and_day_off(self):
        provider = ProviderProfile.objects.create(
            user=self.user, status=ProviderProfile.Status.APPROVED
        )
        day = timezone.localdate() + timedelta(days=2)
        created = self.client.post(
            "/api/v1/providers/me/schedule/",
            {
                "date": day.isoformat(),
                "starts_at": "09:00",
                "ends_at": "12:00",
                "repeat_weekly": True,
                "copy_weekdays": [4],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(provider.weekly_availability.count(), 2)

        schedule = self.client.get(
            "/api/v1/providers/me/schedule/", {"start_date": day.isoformat(), "days": 1}
        )
        self.assertEqual(schedule.status_code, 200)
        self.assertEqual(schedule.json()["data"]["days"][0]["periods"][0]["status"], "available")

        closed = self.client.put(
            f"/api/v1/providers/me/schedule/days/{day.isoformat()}/",
            {"is_closed": True},
            format="json",
        )
        self.assertEqual(closed.status_code, 200)
        self.assertTrue(provider.date_closures.filter(date=day).exists())

    def test_schedule_rejects_overlapping_period_and_past_date(self):
        ProviderProfile.objects.create(user=self.user, status=ProviderProfile.Status.APPROVED)
        day = timezone.localdate() + timedelta(days=2)
        first = self.client.post(
            "/api/v1/providers/me/schedule/",
            {
                "date": day.isoformat(),
                "starts_at": "09:00",
                "ends_at": "12:00",
                "repeat_weekly": False,
            },
            format="json",
        )
        overlap = self.client.post(
            "/api/v1/providers/me/schedule/",
            {
                "date": day.isoformat(),
                "starts_at": "11:00",
                "ends_at": "13:00",
                "repeat_weekly": False,
            },
            format="json",
        )
        past = self.client.post(
            "/api/v1/providers/me/schedule/",
            {
                "date": (timezone.localdate() - timedelta(days=1)).isoformat(),
                "starts_at": "09:00",
                "ends_at": "12:00",
                "repeat_weekly": False,
            },
            format="json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(overlap.status_code, 400)
        self.assertEqual(past.status_code, 400)

    def test_workbench_can_toggle_accepting_orders(self):
        ProviderProfile.objects.create(user=self.user, status=ProviderProfile.Status.APPROVED)
        response = self.client.patch(
            "/api/v1/providers/me/workbench/",
            {"is_accepting_orders": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user.provider_profile.refresh_from_db()
        self.assertFalse(self.user.provider_profile.is_accepting_orders)

    def test_availability_returns_only_configured_future_slots(self):
        user = User.objects.create_user(phone="13800000006", password="test", nickname="小雨")
        provider = ProviderProfile.objects.create(user=user, status=ProviderProfile.Status.APPROVED)
        category = ServiceCategory.objects.create(name="摄影陪伴", slug="photo-availability")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=21800,
        )
        day = timezone.localdate() + timedelta(days=1)
        ProviderWeeklyAvailability.objects.create(
            provider=provider,
            weekday=day.weekday(),
            starts_at=time(13),
            ends_at=time(17),
        )

        response = self.client.get(
            f"/api/v1/providers/{user.public_id}/availability/",
            {"service_id": service.id, "start_date": day.isoformat(), "days": 1},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["earliest"]["starts_at"][11:16], "13:00")
        self.assertEqual(len(data["dates"][0]["slots"]), 5)

    def test_availability_rejects_service_owned_by_another_provider(self):
        first_user = User.objects.create_user(phone="13800000007", password="test")
        second_user = User.objects.create_user(phone="13800000008", password="test")
        ProviderProfile.objects.create(user=first_user, status=ProviderProfile.Status.APPROVED)
        second = ProviderProfile.objects.create(
            user=second_user, status=ProviderProfile.Status.APPROVED
        )
        category = ServiceCategory.objects.create(name="商务陪同", slug="business-availability")
        service = ProviderService.objects.create(
            provider=second,
            category=category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=26800,
            estimated_duration_minutes=120,
        )

        response = self.client.get(
            f"/api/v1/providers/{first_user.public_id}/availability/",
            {"service_id": service.id},
        )

        self.assertEqual(response.status_code, 404)

    def test_availability_does_not_expose_slots_beyond_booking_horizon(self):
        user = User.objects.create_user(phone="13800000010", password="test", nickname="远期达人")
        provider = ProviderProfile.objects.create(user=user, status=ProviderProfile.Status.APPROVED)
        category = ServiceCategory.objects.create(name="远期服务", slug="future-availability")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=16800,
        )
        day = timezone.localdate() + timedelta(days=4)
        ProviderWeeklyAvailability.objects.create(
            provider=provider,
            weekday=day.weekday(),
            starts_at=time(9),
            ends_at=time(18),
        )

        response = self.client.get(
            f"/api/v1/providers/{user.public_id}/availability/",
            {"service_id": service.id, "start_date": day.isoformat(), "days": 1},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"]["earliest"])
        self.assertEqual(response.json()["data"]["dates"][0]["slots"], [])
