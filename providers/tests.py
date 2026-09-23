import uuid
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from backoffice.models import ProviderOrderingSetting
from mediafiles.models import MediaAsset
from orders.models import ProviderOrder, ProviderOrderReview, ProviderOrderSettlement

from .models import (
    ProviderLiveLocation,
    ProviderCategoryGrant,
    ProviderProfile,
    ProviderProfileRevision,
    ProviderService,
    ProviderServiceRevision,
    ProviderWeeklyAvailability,
    ServiceCategory,
)


def create_live_location(
    provider,
    *,
    received_at=None,
    accuracy_m="12.50",
    source_longitude="116.4039810",
    source_latitude="39.9150010",
    position=None,
):
    now = received_at or timezone.now()
    return ProviderLiveLocation.objects.create(
        provider=provider,
        session_id=uuid.uuid4(),
        source_longitude=Decimal(source_longitude),
        source_latitude=Decimal(source_latitude),
        position=position or Point(116.397755, 39.913873, srid=4326),
        accuracy_m=Decimal(accuracy_m),
        located_at=now,
        received_at=now,
    )


def make_provider_eligible(provider):
    if not provider.lifestyle_photo_id:
        provider.lifestyle_photo = MediaAsset.objects.create(
            owner=provider.user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{provider.user.public_id}/{provider.pk}.webp",
        )
    provider.identity_status = ProviderProfile.IdentityStatus.VERIFIED
    provider.onboarding_status = ProviderProfile.OnboardingStatus.APPROVED
    provider.display_name = provider.display_name or provider.user.nickname
    provider.bio = provider.bio or "这是已经完成实名认证和公开资料的达人简介。"
    provider.service_city_code = provider.service_city_code or "130400"
    provider.service_city_name = provider.service_city_name or "邯郸市"
    provider.save()
    return provider


class ProviderModelTests(TestCase):
    def test_provider_service_uses_integer_minor_units_and_live_wgs84_point(self):
        user = User.objects.create_user(phone="13800000001", password="test-password")
        provider = ProviderProfile.objects.create(user=user)
        location = create_live_location(provider)
        category = ServiceCategory.objects.create(name="台球陪玩", slug="billiards")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=17800,
        )

        self.assertEqual(service.price_amount, 17800)
        self.assertEqual(location.position.srid, 4326)
        self.assertEqual(location.source_longitude, Decimal("116.4039810"))

    def test_provider_list_supports_gcj02_distance(self):
        user = User.objects.create_user(
            phone="13800000003", password="test-password", nickname="晓晓"
        )
        provider = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.APPROVED,
            is_accepting_orders=True,
            service_city_code="110100",
            service_city_name="北京市",
        )
        make_provider_eligible(provider)
        create_live_location(provider)
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

        category.is_active = False
        category.save(update_fields=("is_active", "updated_at"))
        inactive_category_response = self.client.get("/api/v1/providers/")
        self.assertEqual(
            inactive_category_response.json()["data"]["pagination"]["total"], 0
        )
        category.is_active = True
        category.save(update_fields=("is_active", "updated_at"))

        ProviderLiveLocation.objects.filter(provider=provider).update(
            received_at=timezone.now() - timedelta(minutes=31)
        )
        stale_response = self.client.get(
            "/api/v1/providers/",
            {
                "city_code": "110100",
                "longitude": "116.4039810",
                "latitude": "39.9150010",
                "ordering": "distance",
            },
        )
        stale_item = stale_response.json()["data"]["items"][0]
        self.assertEqual(stale_response.json()["data"]["pagination"]["total"], 1)
        self.assertFalse(stale_item["is_online"])
        self.assertIsNotNone(stale_item["distance_km"])

        setting = ProviderOrderingSetting.current()
        setting.location_timeout_minutes = 0
        setting.save(update_fields=("location_timeout_minutes", "updated_at"))
        no_expiry_response = self.client.get("/api/v1/providers/")
        self.assertTrue(no_expiry_response.json()["data"]["items"][0]["is_online"])

    def test_provider_list_excludes_provider_outside_own_service_radius(self):
        category = ServiceCategory.objects.create(name="桌游陪玩", slug="radius-filter")
        nearby_user = User.objects.create_user(
            phone="13800000031", password="test-password", nickname="附近达人"
        )
        nearby = ProviderProfile.objects.create(
            user=nearby_user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
            max_service_radius_km=10,
        )
        make_provider_eligible(nearby)
        create_live_location(
            nearby,
            source_longitude="114.5240070",
            source_latitude="36.6074460",
            position=Point(114.518, 36.607, srid=4326),
        )
        ProviderService.objects.create(
            provider=nearby,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=15800,
        )

        distant_user = User.objects.create_user(
            phone="13800000032", password="test-password", nickname="远方达人"
        )
        distant = ProviderProfile.objects.create(
            user=distant_user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
            max_service_radius_km=70,
        )
        make_provider_eligible(distant)
        create_live_location(distant)
        ProviderService.objects.create(
            provider=distant,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=16800,
        )

        response = self.client.get(
            "/api/v1/providers/",
            {
                "city_code": "130400",
                "longitude": "114.5240070",
                "latitude": "36.6074460",
                "ordering": "distance",
            },
        )

        self.assertEqual(response.status_code, 200)
        items = response.json()["data"]["items"]
        self.assertEqual([item["nickname"] for item in items], ["附近达人"])

    def test_provider_list_supports_advanced_filters(self):
        user = User.objects.create_user(
            phone="13800000033",
            password="test-password",
            nickname="甜甜桌游搭子",
            gender=User.Gender.FEMALE,
        )
        provider = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.APPROVED,
            is_accepting_orders=True,
            service_city_code="130400",
            service_city_name="邯郸市",
            bio="擅长桌游规则讲解",
            rating=Decimal("4.80"),
        )
        make_provider_eligible(provider)
        category = ServiceCategory.objects.create(name="桌游陪玩", slug="board-games-filter")
        ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=15800,
        )
        create_live_location(provider)

        matched = self.client.get(
            "/api/v1/providers/",
            {
                "keyword": "桌游",
                "category": category.slug,
                "city_code": "130400",
                "gender": "female",
                "online_only": "true",
                "min_rating": "4.80",
                "max_price_amount": "16000",
            },
        )
        self.assertEqual(matched.status_code, 200)
        self.assertEqual(matched.json()["data"]["pagination"]["total"], 1)

        for params in (
            {"keyword": "台球"},
            {"gender": "male"},
            {"min_rating": "4.90"},
            {"max_price_amount": "15000"},
        ):
            response = self.client.get("/api/v1/providers/", params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["pagination"]["total"], 0)

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
        make_provider_eligible(provider)
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

    def test_provider_reviews_only_return_visible_reviews_and_summary(self):
        provider_user = User.objects.create_user(
            phone="13800000006", password="test-password", nickname="甜甜"
        )
        customer = User.objects.create_user(
            phone="13800000007", password="test-password", nickname="小雨"
        )
        provider = ProviderProfile.objects.create(
            user=provider_user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
            rating="5.00",
        )
        make_provider_eligible(provider)
        category = ServiceCategory.objects.create(name="桌游陪玩", slug="board-game-review")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=12800,
        )
        now = timezone.now()
        visible_order = ProviderOrder.objects.create(
            order_no="DZY-REVIEW-VISIBLE",
            customer=customer,
            provider=provider,
            service=service,
            provider_name_snapshot=provider_user.nickname,
            service_name_snapshot=category.name,
            billing_type_snapshot=ProviderOrder.BillingType.HOURLY,
            unit_price_amount=12800,
            starts_at=now,
            ends_at=now + timedelta(hours=2),
            duration_minutes=120,
            meeting_address="人民路",
            contact_name="张三",
            contact_phone="13800000007",
            service_fee_amount=25600,
            payable_amount=25600,
            payment_expires_at=now,
            status=ProviderOrder.Status.COMPLETED,
        )
        hidden_order = ProviderOrder.objects.create(
            order_no="DZY-REVIEW-HIDDEN",
            customer=customer,
            provider=provider,
            service=service,
            provider_name_snapshot=provider_user.nickname,
            service_name_snapshot=category.name,
            billing_type_snapshot=ProviderOrder.BillingType.HOURLY,
            unit_price_amount=12800,
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=1, hours=2),
            duration_minutes=120,
            meeting_address="人民路",
            contact_name="张三",
            contact_phone="13800000007",
            service_fee_amount=25600,
            payable_amount=25600,
            payment_expires_at=now,
            status=ProviderOrder.Status.COMPLETED,
        )
        image = MediaAsset.objects.create(
            owner=customer,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.REVIEW_IMAGE,
            status=MediaAsset.Status.UPLOADED,
            object_key="public/review-images/public-review.webp",
        )
        visible_review = ProviderOrderReview.objects.create(
            order=visible_order,
            customer=customer,
            provider=provider,
            rating=5,
            content="服务很好",
            is_anonymous=True,
            audit_status=ProviderOrderReview.AuditStatus.APPROVED,
        )
        visible_review.images.add(image)
        unpublished_review = ProviderOrderReview.objects.create(
            order=hidden_order,
            customer=customer,
            provider=provider,
            rating=1,
            content="不应公开",
            is_visible=False,
            audit_status=ProviderOrderReview.AuditStatus.APPROVED,
        )

        response = self.client.get(f"/api/v1/providers/{provider_user.public_id}/reviews/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["pagination"]["total"], 1)
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(data["summary"]["distribution"]["5"], 1)
        self.assertEqual(data["summary"]["distribution"]["1"], 0)
        self.assertEqual(data["items"][0]["customer_name"], "匿名用户")
        self.assertEqual(len(data["items"][0]["image_urls"]), 1)

        filtered = self.client.get(
            f"/api/v1/providers/{provider_user.public_id}/reviews/", {"rating": 4}
        )
        self.assertEqual(filtered.json()["data"]["pagination"]["total"], 0)

        # 即使公开开关开启，待审和驳回内容也不能进入公开列表或评分统计。
        for audit_status in (
            ProviderOrderReview.AuditStatus.PENDING,
            ProviderOrderReview.AuditStatus.REJECTED,
        ):
            unpublished_review.audit_status = audit_status
            unpublished_review.is_visible = True
            unpublished_review.save()
            response = self.client.get(
                f"/api/v1/providers/{provider_user.public_id}/reviews/"
            )
            self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
            self.assertEqual(response.json()["data"]["summary"]["distribution"]["1"], 0)

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
        draft = self.client.patch(
            "/api/v1/providers/me/application/",
            {
                "application_real_name": "张小雨",
                "application_birth_date": "1998-06-18",
                "lifestyle_photo_id": str(MediaAsset.objects.create(
                    owner=self.user,
                    scope=MediaAsset.Scope.PUBLIC,
                    category=MediaAsset.Category.PROVIDER_PHOTO,
                    status=MediaAsset.Status.UPLOADED,
                    object_key=f"public/provider-photos/{self.user.public_id}/application.webp",
                ).id),
                "bio": "我熟悉本地路线，也喜欢摄影和旅行。",
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

    def test_provider_application_requires_real_name_birth_date_and_lifestyle_photo(self):
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
        self.assertIn("真实姓名", str(submitted.data))

    def test_provider_application_draft_allows_clearing_birth_date(self):
        response = self.client.patch(
            "/api/v1/providers/me/application/",
            {"application_birth_date": None},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"]["application_birth_date"])

    def test_provider_profile_rejects_another_users_lifestyle_photo(self):
        ProviderProfile.objects.create(user=self.user, status=ProviderProfile.Status.APPROVED)
        other = User.objects.create_user(phone="13800000029", password="test-password")
        lifestyle_photo = MediaAsset.objects.create(
            owner=other,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{other.public_id}/lifestyle.webp",
        )

        response = self.client.patch(
            "/api/v1/providers/me/profile/",
            {"lifestyle_photo_id": str(lifestyle_photo.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_approved_applicant_can_submit_private_identity_materials(self):
        profile = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
        )
        photos = [
            MediaAsset.objects.create(
                owner=self.user,
                scope=MediaAsset.Scope.PRIVATE,
                category=MediaAsset.Category.IDENTITY,
                status=MediaAsset.Status.UPLOADED,
                object_key=f"private/provider-identities/{self.user.public_id}/{kind}.webp",
            )
            for kind in ("front", "back", "face")
        ]

        saved = self.client.patch(
            "/api/v1/providers/me/identity/",
            {
                "identity_real_name": "张三",
                "id_number": "130400199001011234",
                "identity_front_photo_id": str(photos[0].id),
                "identity_back_photo_id": str(photos[1].id),
                "identity_face_photo_id": str(photos[2].id),
            },
            format="json",
        )
        submitted = self.client.post(
            "/api/v1/providers/me/identity/submit/", {}, format="json"
        )

        self.assertEqual(saved.status_code, 200)
        self.assertNotIn("id_number", saved.json()["data"])
        self.assertEqual(saved.json()["data"]["identity_number_masked"], "1304**********1234")
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(
            submitted.json()["data"]["identity_status"],
            ProviderProfile.IdentityStatus.PENDING,
        )
        profile.refresh_from_db()
        self.assertNotIn("130400199001011234", profile.identity_number_digest)

    def test_unverified_provider_cannot_start_accepting_orders(self):
        profile = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            bio="这是已经完善但尚未完成实名认证的达人资料。",
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        profile.lifestyle_photo = MediaAsset.objects.create(
            owner=self.user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{self.user.public_id}/unverified.webp",
        )
        profile.onboarding_status = ProviderProfile.OnboardingStatus.APPROVED
        profile.display_name = self.user.nickname
        profile.save(update_fields=("lifestyle_photo", "onboarding_status", "display_name", "updated_at"))
        category = ServiceCategory.objects.create(
            name="未实名接单测试",
            slug="unverified-online",
        )
        ProviderService.objects.create(
            provider=profile,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=16800,
        )

        response = self.client.post(
            "/api/v1/providers/me/online/start/",
            {
                "longitude": "114.5389610",
                "latitude": "36.6256570",
                "accuracy_m": "18.50",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("实名认证", str(response.data))
        profile.refresh_from_db()
        self.assertFalse(profile.is_accepting_orders)

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
        profile = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
        )
        category = ServiceCategory.objects.create(name="城市漫游", slug="city-walk-manage")
        ProviderCategoryGrant.objects.create(provider=profile, category=category)
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
        self.assertIsNone(created.json()["data"]["id"])
        self.assertEqual(created.json()["data"]["review_status"], "pending")
        self.assertFalse(ProviderService.objects.filter(provider=profile).exists())
        ProviderServiceRevision.objects.filter(provider=profile).update(
            status=ProviderServiceRevision.Status.REJECTED
        )
        service = ProviderService.objects.create(
            provider=profile,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=12800,
        )

        updated = self.client.patch(
            f"/api/v1/providers/me/services/{service.id}/",
            {"price_amount": 15800},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["data"]["price_amount"], 15800)

        self.assertEqual(updated.json()["data"]["review_status"], "pending")
        service.refresh_from_db()
        self.assertEqual(service.price_amount, 12800)
        disabled = self.client.delete(f"/api/v1/providers/me/services/{service.id}/")
        self.assertEqual(disabled.status_code, 204)
        self.assertFalse(ProviderService.objects.get(id=service.id).is_active)

    def test_service_submission_requires_grant_and_enforces_billing_price_range(self):
        profile = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
        )
        category = ServiceCategory.objects.create(
            name="限价服务",
            slug="bounded-price-service",
            hourly_min_price_amount=10000,
            hourly_max_price_amount=20000,
            per_session_min_price_amount=30000,
            per_session_max_price_amount=50000,
        )
        payload = {
            "category_id": category.id,
            "billing_type": ProviderService.BillingType.HOURLY,
            "price_amount": 15000,
            "estimated_duration_minutes": 120,
            "description": "价格区间测试服务",
        }

        no_grant = self.client.post("/api/v1/providers/me/services/", payload, format="json")
        self.assertEqual(no_grant.status_code, 400)
        ProviderCategoryGrant.objects.create(provider=profile, category=category)
        below_range = self.client.post(
            "/api/v1/providers/me/services/",
            {**payload, "price_amount": 9999},
            format="json",
        )
        accepted = self.client.post(
            "/api/v1/providers/me/services/",
            {**payload, "price_amount": 10000},
            format="json",
        )

        self.assertEqual(below_range.status_code, 400)
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(accepted.json()["data"]["review_status"], "pending")

    def test_profile_change_stays_pending_without_changing_public_profile(self):
        photo = MediaAsset.objects.create(
            owner=self.user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{self.user.public_id}/profile-review.webp",
        )
        profile = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
            display_name="旧达人名",
            bio="这是当前已经审核通过并公开展示的达人简介。",
            lifestyle_photo=photo,
            service_city_code="130400",
            service_city_name="邯郸市",
        )

        response = self.client.patch(
            "/api/v1/providers/me/profile/",
            {
                "display_name": "新达人名",
                "bio": "这是等待后台审核通过后才能生效的新达人简介。",
                "lifestyle_photo_id": str(photo.id),
                "service_city_code": "110100",
                "service_city_name": "北京市",
                "max_service_radius_km": 20,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["review_status"], "pending")
        profile.refresh_from_db()
        self.assertEqual(profile.display_name, "旧达人名")
        self.assertEqual(profile.service_city_name, "邯郸市")
        self.assertTrue(
            ProviderProfileRevision.objects.filter(
                provider=profile,
                status=ProviderProfileRevision.Status.PENDING,
                display_name="新达人名",
            ).exists()
        )

    def test_latest_approved_revisions_do_not_expose_historical_rejections(self):
        photo = MediaAsset.objects.create(
            owner=self.user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{self.user.public_id}/approved-current.webp",
        )
        profile = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
            display_name="当前达人名",
            bio="这是当前已经审核通过并正式生效的达人简介。",
            lifestyle_photo=photo,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        ProviderProfileRevision.objects.create(
            provider=profile,
            display_name="已驳回达人名",
            bio="这是历史上已经被后台驳回的达人简介内容。",
            lifestyle_photo=photo,
            service_city_code="110100",
            service_city_name="北京市",
            status=ProviderProfileRevision.Status.REJECTED,
            rejection_reason="历史驳回",
        )
        ProviderProfileRevision.objects.create(
            provider=profile,
            display_name="当前达人名",
            bio=profile.bio,
            lifestyle_photo=photo,
            service_city_code="130400",
            service_city_name="邯郸市",
            status=ProviderProfileRevision.Status.APPROVED,
        )
        category = ServiceCategory.objects.create(
            name="审核历史测试服务", slug="revision-history-service"
        )
        service = ProviderService.objects.create(
            provider=profile,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=15000,
            description="当前已生效服务",
        )
        ProviderServiceRevision.objects.create(
            provider=profile,
            category=category,
            action=ProviderServiceRevision.Action.CREATE,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=9000,
            description="历史驳回服务",
            status=ProviderServiceRevision.Status.REJECTED,
            rejection_reason="历史驳回",
        )
        ProviderServiceRevision.objects.create(
            provider=profile,
            service=service,
            category=category,
            action=ProviderServiceRevision.Action.CREATE,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=15000,
            description="当前已生效服务",
            status=ProviderServiceRevision.Status.APPROVED,
        )

        profile_response = self.client.get("/api/v1/providers/me/profile/")
        services_response = self.client.get("/api/v1/providers/me/services/")

        self.assertEqual(profile_response.status_code, 200)
        self.assertEqual(profile_response.json()["data"]["display_name"], "当前达人名")
        self.assertEqual(profile_response.json()["data"]["review_status"], "approved")
        self.assertEqual(services_response.status_code, 200)
        service_items = services_response.json()["data"]["items"]
        self.assertEqual(len(service_items), 1)
        self.assertEqual(service_items[0]["price_amount"], 15000)
        self.assertEqual(service_items[0]["review_status"], "approved")

    def test_verified_legacy_provider_auto_submits_combined_onboarding(self):
        photo = MediaAsset.objects.create(
            owner=self.user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{self.user.public_id}/legacy-onboarding.webp",
        )
        profile = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            identity_status=ProviderProfile.IdentityStatus.VERIFIED,
            onboarding_status=ProviderProfile.OnboardingStatus.INCOMPLETE,
            display_name="历史达人",
            bio="这是历史达人等待补齐综合开通审核的资料。",
            lifestyle_photo=photo,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        category = ServiceCategory.objects.create(
            name="历史达人服务", slug="legacy-onboarding-service"
        )
        ProviderCategoryGrant.objects.create(provider=profile, category=category)
        profile_response = self.client.patch(
            "/api/v1/providers/me/profile/",
            {
                "display_name": "历史达人新名称",
                "bio": "这是历史达人补齐后提交审核的新版达人资料。",
                "lifestyle_photo_id": str(photo.id),
                "service_city_code": "130400",
                "service_city_name": "邯郸市",
            },
            format="json",
        )
        service_response = self.client.post(
            "/api/v1/providers/me/services/",
            {
                "category_id": category.id,
                "billing_type": ProviderService.BillingType.HOURLY,
                "price_amount": 15000,
                "description": "历史达人补齐的服务配置",
            },
            format="json",
        )

        self.assertEqual(profile_response.status_code, 200)
        self.assertEqual(service_response.status_code, 201)
        profile.refresh_from_db()
        self.assertEqual(
            profile.onboarding_status,
            ProviderProfile.OnboardingStatus.PENDING_REVIEW,
        )

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
        self.assertEqual(provider.weekly_availability.count(), len({day.weekday(), 4}))

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

    def test_approved_provider_can_start_update_and_stop_online_session(self):
        provider = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        make_provider_eligible(provider)
        category = ServiceCategory.objects.create(name="城市陪伴", slug="online-session")
        ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=16800,
        )

        started = self.client.post(
            "/api/v1/providers/me/online/start/",
            {
                "longitude": "114.538961012345",
                "latitude": "36.625657049876",
                "accuracy_m": "18.5051",
            },
            format="json",
        )
        self.assertEqual(started.status_code, 200)
        self.assertTrue(started.json()["data"]["is_online"])
        session_id = started.json()["data"]["session_id"]
        provider.refresh_from_db()
        self.assertTrue(provider.is_accepting_orders)
        self.assertEqual(provider.live_location.source_longitude, Decimal("114.5389610"))
        self.assertEqual(provider.live_location.source_latitude, Decimal("36.6256570"))
        self.assertEqual(provider.live_location.accuracy_m, Decimal("18.51"))

        workbench = self.client.get("/api/v1/providers/me/workbench/")
        self.assertEqual(workbench.status_code, 200)
        self.assertTrue(workbench.json()["data"]["is_online"])
        self.assertEqual(workbench.json()["data"]["session_id"], session_id)
        self.assertEqual(workbench.json()["data"]["online_timeout_minutes"], 30)
        self.assertEqual(workbench.json()["data"]["recommended_report_interval_seconds"], 300)

        ProviderLiveLocation.objects.filter(provider=provider).update(
            received_at=timezone.now() - timedelta(minutes=31)
        )
        stale_workbench = self.client.get("/api/v1/providers/me/workbench/")
        self.assertFalse(stale_workbench.json()["data"]["is_online"])

        updated = self.client.put(
            "/api/v1/providers/me/online/location/",
            {
                "session_id": session_id,
                "longitude": "114.539961056789",
                "latitude": "36.626657043210",
                "accuracy_m": "15.0049",
            },
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["data"]["is_online"])
        provider.live_location.refresh_from_db()
        self.assertEqual(provider.live_location.source_longitude, Decimal("114.5399611"))
        self.assertEqual(provider.live_location.source_latitude, Decimal("36.6266570"))
        self.assertEqual(provider.live_location.accuracy_m, Decimal("15.00"))

        stopped = self.client.post("/api/v1/providers/me/online/stop/", {}, format="json")
        self.assertEqual(stopped.status_code, 200)
        self.assertFalse(stopped.json()["data"]["is_online"])
        self.assertIsNone(stopped.json()["data"]["session_id"])

    def test_workbench_returns_month_metrics_trend_and_upcoming_order_details(self):
        provider = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        customer = User.objects.create_user(
            phone="13800000022", password="test-password", nickname="订单用户"
        )
        category = ServiceCategory.objects.create(name="旅游陪伴", slug="travel-workbench")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=16800,
        )
        now = timezone.localtime()
        starts_at = now + timedelta(days=1)
        order = ProviderOrder.objects.create(
            order_no="WB202608280001",
            customer=customer,
            provider=provider,
            service=service,
            provider_name_snapshot=self.user.nickname,
            service_name_snapshot=category.name,
            billing_type_snapshot=ProviderOrder.BillingType.HOURLY,
            unit_price_amount=16800,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            duration_minutes=120,
            meeting_location_name="邯郸美乐城",
            meeting_address="人民东路456号",
            contact_name="张",
            contact_gender=ProviderOrder.ContactGender.MS,
            contact_phone="13812346688",
            service_fee_amount=33600,
            payable_amount=33600,
            status=ProviderOrder.Status.PENDING_SERVICE,
            payment_expires_at=now + timedelta(hours=1),
            paid_at=now,
        )
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        completed_starts_at = max(now - timedelta(hours=2), week_start)
        ProviderOrder.objects.create(
            order_no="WB202608280002",
            customer=customer,
            provider=provider,
            service=service,
            provider_name_snapshot=self.user.nickname,
            service_name_snapshot=category.name,
            billing_type_snapshot=ProviderOrder.BillingType.HOURLY,
            unit_price_amount=16800,
            starts_at=completed_starts_at,
            ends_at=completed_starts_at + timedelta(hours=2),
            duration_minutes=120,
            meeting_location_name="丛台公园",
            meeting_address="中华北大街159号",
            contact_name="李",
            contact_gender=ProviderOrder.ContactGender.MR,
            contact_phone="13812346689",
            service_fee_amount=33600,
            payable_amount=33600,
            status=ProviderOrder.Status.COMPLETED,
            payment_expires_at=now - timedelta(days=2),
            paid_at=now,
        )

        response = self.client.get("/api/v1/providers/me/workbench/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["month_income_amount"], 67200)
        self.assertEqual(data["month_order_count"], 2)
        self.assertEqual(data["month_service_hours"], 2.0)
        self.assertEqual(data["pending_acceptance_order_count"], 0)
        self.assertEqual(len(data["last_7_days_service_trend"]), 7)
        self.assertEqual(
            sum(item["service_hours"] for item in data["last_7_days_service_trend"]),
            2.0,
        )
        self.assertEqual(data["upcoming_order"]["public_id"], str(order.public_id))
        self.assertEqual(data["upcoming_order"]["customer_name"], "张")
        self.assertEqual(data["upcoming_order"]["customer_gender_label"], "女士")
        self.assertEqual(data["upcoming_order"]["meeting_location_name"], "邯郸美乐城")

    def test_income_returns_real_settlement_ledger(self):
        provider = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        customer = User.objects.create_user(
            phone="13800000023", password="test-password", nickname="收入订单用户"
        )
        category = ServiceCategory.objects.create(
            name="收入测试服务", slug="provider-income-ledger"
        )
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=16800,
        )
        now = timezone.now()
        order = ProviderOrder.objects.create(
            order_no="INCOME202609050001",
            customer=customer,
            provider=provider,
            service=service,
            provider_name_snapshot=self.user.nickname,
            service_name_snapshot=category.name,
            billing_type_snapshot=ProviderOrder.BillingType.HOURLY,
            unit_price_amount=16800,
            starts_at=now - timedelta(days=2, hours=2),
            ends_at=now - timedelta(days=2),
            duration_minutes=120,
            meeting_location_name="邯郸美乐城",
            meeting_address="人民东路456号",
            contact_name="张三",
            contact_gender=ProviderOrder.ContactGender.MR,
            contact_phone="13812346688",
            service_fee_amount=33600,
            payable_amount=33600,
            status=ProviderOrder.Status.COMPLETED,
            payment_expires_at=now - timedelta(days=3),
            paid_at=now - timedelta(days=3),
            customer_confirmed_at=now - timedelta(days=1),
        )
        settlement = ProviderOrderSettlement.objects.create(
            order=order,
            provider=provider,
            paid_amount=33600,
            refunded_amount=0,
            net_service_fee_amount=33600,
            net_transport_fee_amount=0,
            net_other_fee_amount=0,
            platform_commission_rate=Decimal("20.00"),
            platform_commission_amount=6720,
            provider_service_income_amount=26880,
            provider_settlement_amount=26880,
            status=ProviderOrderSettlement.Status.SETTLED,
            frozen_at=now - timedelta(days=1),
            freeze_until=now,
            settled_at=now,
        )

        response = self.client.get("/api/v1/providers/me/income/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["summary"]["month_income_amount"], 26880)
        self.assertEqual(data["summary"]["pending_amount"], 0)
        self.assertEqual(data["summary"]["settled_amount"], 26880)
        self.assertEqual(data["summary"]["month_order_count"], 1)
        self.assertEqual(data["items"][0]["settlement_no"], settlement.settlement_no)
        self.assertEqual(data["items"][0]["settlement_amount"], 26880)

    def test_provider_needs_active_service_and_accurate_first_location_to_start(self):
        provider = ProviderProfile.objects.create(
            user=self.user,
            status=ProviderProfile.Status.APPROVED,
        )
        make_provider_eligible(provider)

        no_service = self.client.post(
            "/api/v1/providers/me/online/start/",
            {"longitude": "114.5389610", "latitude": "36.6256570", "accuracy_m": "20"},
            format="json",
        )
        inaccurate = self.client.post(
            "/api/v1/providers/me/online/start/",
            {"longitude": "114.5389610", "latitude": "36.6256570", "accuracy_m": "201"},
            format="json",
        )

        self.assertEqual(no_service.status_code, 400)
        self.assertIn("至少一项服务", str(no_service.json()))
        self.assertEqual(inaccurate.status_code, 400)

    def test_availability_returns_only_configured_future_slots(self):
        user = User.objects.create_user(phone="13800000006", password="test", nickname="小雨")
        provider = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.APPROVED,
            is_accepting_orders=True,
        )
        make_provider_eligible(provider)
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
        make_provider_eligible(provider)
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
