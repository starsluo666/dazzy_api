from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, RequestFactory, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from activities.models import Activity, ActivityCategory, ActivityPublishOrder, ActivityParticipationPaymentOrder
from activities.services import create_activity_draft, get_or_create_publish_order, get_or_create_participation_order
from backoffice.models import PlatformOperationSetting
from backoffice.services import review_provider_application, review_provider_profile_revision, review_provider_onboarding, review_activity
from mediafiles.models import MediaAsset
from orders.services import validate_booking
from providers.models import ProviderProfile, ProviderProfileRevision, ProviderService, ProviderServiceRevision, ServiceCategory
from providers.services import save_provider_application, submit_provider_application, submit_provider_profile_revision, start_provider_online
from providers.presence import provider_is_online, online_provider_query
from providers.tests import make_provider_eligible, create_live_location
from .discovery import require_open_city


@override_settings(DEBUG=True)
class ServiceCityValidationTests(TestCase):
    def setUp(self):
        self.config = PlatformOperationSetting.objects.create(discovery_cities=[{"city_code": "130400", "city_name": "邯郸市"}])
        self.user = User.objects.create_user(phone="19900009701", gender="female", nickname="测试达人")
        self.customer = User.objects.create_user(phone="19900009702")
        self.admin = User.objects.create_superuser(phone="19900009703", password="test-only")
        self.photo = MediaAsset.objects.create(owner=self.user, scope="public", category="provider_photo", status="uploaded", provider_photo_cropped=True, object_key="synthetic/city-photo.webp")
        self.profile = ProviderProfile.objects.create(user=self.user, status="draft", application_real_name="测试达人", application_birth_date=date(1996, 1, 1), lifestyle_photo=self.photo, service_city_code="130400", service_city_name="邯郸市")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.review_args = dict(decision="approve", reason="", actor=self.admin, access=SimpleNamespace(all_data=True, member=None), request=RequestFactory().post("/"))

    def close_city(self):
        self.config.discovery_cities = [{"city_code": "510100", "city_name": "成都市"}]
        self.config.save(update_fields=["discovery_cities"])

    def eligible(self):
        self.profile.status = "approved"
        self.profile.is_accepting_orders = True
        make_provider_eligible(self.profile)
        category = ServiceCategory.objects.create(name="测试", slug="city-test")
        service = ProviderService.objects.create(provider=self.profile, category=category, billing_type="hourly", price_amount=5000, is_active=True)
        create_live_location(self.profile)
        return service

    def test_configuration_is_live_and_name_is_authoritative(self):
        self.assertEqual(require_open_city(" 130400 "), ("130400", "邯郸市"))
        for code in ("", "110100", "310100"):
            with self.assertRaises(ValidationError):
                require_open_city(code)
        self.close_city()
        with self.assertRaises(ValidationError):
            require_open_city("130400")
        self.assertEqual(require_open_city("510100")[1], "成都市")
        self.config.discovery_cities = []
        self.config.save(update_fields=["discovery_cities"])
        with self.assertRaises(ValidationError):
            require_open_city("130400")

    def test_application_api_checks_code_and_canonicalizes_name_even_for_partial_patch(self):
        url = "/api/v1/providers/me/application/"
        for code in ("110100", "310100", "999999"):
            self.assertEqual(self.client.patch(url, {"service_city_code": code, "service_city_name": "邯郸市"}, format="json").status_code, 400)
        response = self.client.patch(url, {"service_city_name": "北京市"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["service_city_name"], "邯郸市")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.service_city_code, "130400")
        self.close_city()
        with self.assertRaises(ValidationError):
            submit_provider_application(user=self.user)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, "draft")
        updated = save_provider_application(user=self.user, data={"service_city_code": "510100", "service_city_name": "假名称"})
        self.assertEqual(updated.service_city_name, "成都市")
        self.assertEqual(submit_provider_application(user=self.user).status, "pending")

    def test_draft_can_clear_city_but_cannot_submit_without_city(self):
        draft = save_provider_application(user=self.user, data={"service_city_code": "", "service_city_name": "北京市"})
        self.assertEqual(draft.service_city_name, "")
        with self.assertRaises(ValidationError):
            submit_provider_application(user=self.user)

    def test_application_review_rechecks_city_but_rejection_remains_possible(self):
        self.profile.status = "pending"
        self.profile.save()
        self.close_city()
        with self.assertRaises(ValidationError):
            review_provider_application(profile_id=self.profile.pk, allowed_category_ids=[], **self.review_args)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, "pending")
        result = review_provider_application(profile_id=self.profile.pk, allowed_category_ids=[], **{**self.review_args, "decision": "reject", "reason": "请选择开放城市"})
        self.assertEqual(result.status, "rejected")

    def test_profile_submission_and_review_recheck_city(self):
        self.eligible()
        with self.assertRaises(ValidationError):
            submit_provider_profile_revision(provider=self.profile, data={"service_city_code": "110100"})
        self.assertFalse(ProviderProfileRevision.objects.exists())
        revision = submit_provider_profile_revision(provider=self.profile, data={"service_city_name": "假名称"})
        self.assertEqual(revision.service_city_name, "邯郸市")
        self.close_city()
        with self.assertRaises(ValidationError):
            review_provider_profile_revision(revision_id=revision.pk, **self.review_args)
        revision.refresh_from_db()
        self.assertEqual(revision.status, "pending")
        result = review_provider_profile_revision(revision_id=revision.pk, **{**self.review_args, "decision": "reject", "reason": "城市已停用"})
        self.assertEqual(result.status, "rejected")

    def test_combined_onboarding_checks_revision_city(self):
        service = self.eligible()
        revision = submit_provider_profile_revision(provider=self.profile, data={})
        ProviderServiceRevision.objects.create(provider=self.profile, category=service.category, price_amount=5000, billing_type="hourly")
        self.profile.onboarding_status = "pending_review"
        self.profile.save()
        self.close_city()
        with self.assertRaises(ValidationError):
            review_provider_onboarding(profile_id=self.profile.pk, **self.review_args)
        revision.refresh_from_db()
        self.assertEqual(revision.status, "pending")

    def test_city_closure_blocks_online_presence_and_new_booking(self):
        service = self.eligible()
        self.assertTrue(provider_is_online(self.profile))
        self.close_city()
        self.assertFalse(provider_is_online(self.profile))
        self.assertFalse(ProviderProfile.objects.filter(online_provider_query()).exists())
        with self.assertRaises(ValidationError):
            start_provider_online(provider=self.profile, data={})
        with self.assertRaises(ValidationError):
            validate_booking(service, timezone.now() + timedelta(hours=3), 120)

    def activity_data(self):
        category, _ = ActivityCategory.objects.get_or_create(slug="city-activity", defaults={"name": "活动"})
        starts = timezone.now() + timedelta(days=3)
        return dict(category_slug=category, title="测试活动", starts_at=starts, ends_at=starts + timedelta(hours=3), formation_deadline=starts - timedelta(hours=12), meeting_place_name="测试地点", meeting_address="测试地址", city_code="130400", city_name="假名称", longitude="114.5", latitude="36.6", capacity=6, min_participants=2, description="测试活动内容", participation_rules="准时参加", aa_principal_amount=5000, refund_template_version="standard-v1")

    def test_activity_creation_rejects_missing_or_closed_city_and_uses_configured_name(self):
        for code in ("", "110100", "310100"):
            payload = self.activity_data()
            payload["city_code"] = code
            with self.assertRaises(ValidationError):
                create_activity_draft(organizer=self.user, validated_data=payload)
        self.assertFalse(Activity.objects.exists())
        activity = create_activity_draft(organizer=self.user, validated_data=self.activity_data())
        self.assertEqual(activity.city_name, "邯郸市")
        payload = self.activity_data()
        payload.update(category_slug=payload["category_slug"].slug, city_code="110100")
        response = self.client.post("/api/v1/activities/", payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("city_code", response.data)

    def test_activity_old_payment_recovery_survives_city_closure_but_new_orders_do_not(self):
        activity = create_activity_draft(organizer=self.user, validated_data=self.activity_data())
        publish = get_or_create_publish_order(activity_id=activity.pk, user=self.user)
        activity.status = "recruiting"
        activity.save()
        _, participation, _ = get_or_create_participation_order(activity_id=activity.pk, user=self.customer, channel="wechat")
        self.close_city()
        # Recovery does not create another order or lose the original payment reference.
        activity.status = "draft"
        activity.save()
        self.assertEqual(get_or_create_publish_order(activity_id=activity.pk, user=self.user).pk, publish.pk)
        activity.status = "recruiting"
        activity.save()
        self.assertEqual(get_or_create_participation_order(activity_id=activity.pk, user=self.customer, channel="wechat")[1].pk, participation.pk)
        with self.assertRaises(ValidationError):
            get_or_create_participation_order(activity_id=activity.pk, user=self.admin, channel="wechat")
        activity.status = "draft"
        activity.save()
        publish.status = "cancelled"
        publish.save()
        with self.assertRaises(ValidationError):
            get_or_create_publish_order(activity_id=activity.pk, user=self.user)
        self.assertEqual(ActivityPublishOrder.objects.count(), 1)
        self.assertEqual(ActivityParticipationPaymentOrder.objects.count(), 1)

    def test_activity_approval_rechecks_city_without_blocking_rejection(self):
        activity = create_activity_draft(organizer=self.user, validated_data=self.activity_data())
        publish = get_or_create_publish_order(activity_id=activity.pk, user=self.user)
        publish.status = "paid"
        publish.save()
        activity.status = "pending_review"
        activity.save()
        self.close_city()
        with self.assertRaises(ValidationError):
            review_activity(activity_id=activity.pk, **self.review_args)
        activity.refresh_from_db()
        self.assertEqual(activity.status, "pending_review")
        with patch("backoffice.services.refund_publish_order") as refund:
            review_activity(activity_id=activity.pk, **{**self.review_args, "decision": "reject", "reason": "城市未开放"})
        refund.assert_called_once()
