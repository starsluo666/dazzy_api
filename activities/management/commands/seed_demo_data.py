from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from config.geospatial import gcj02_to_wgs84
from providers.models import (
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)

from ...models import Activity, ActivityCategory


PROVIDER_CATEGORIES = (
    ("旅游陪伴", "travel"),
    ("台球陪玩", "billiards"),
    ("麻将陪玩", "mahjong"),
    ("桌游陪玩", "board-games"),
    ("商务陪同", "business"),
)
ACTIVITY_CATEGORIES = (
    ("台球", "billiards"),
    ("麻将", "mahjong"),
    ("桌游", "board-games"),
    ("旅行", "travel"),
    ("其他", "other"),
)
PROVIDERS = (
    ("13810000001", "晓晓", "billiards", "17800", "116.4039810", "39.9150010", "4.90"),
    ("13810000002", "小雨", "travel", "16800", "116.4172010", "39.9212110", "4.80"),
    ("13810000003", "甜甜", "board-games", "15800", "116.3891200", "39.9088300", "4.90"),
    ("13810000004", "可可", "business", "18800", "116.4294300", "39.9131200", "4.70"),
)
MULTI_SERVICE_PROVIDER = {
    "phone": "13810000005",
    "nickname": "多多",
    "longitude": "116.4118600",
    "latitude": "39.9166300",
    "services": (
        ("travel", ProviderService.BillingType.HOURLY, 17800, None, "城市漫游与旅行陪伴"),
        ("board-games", ProviderService.BillingType.PER_SESSION, 21800, 180, "桌游教学与欢乐组局"),
        ("business", ProviderService.BillingType.HOURLY, 26800, None, "商务出行与活动陪同"),
    ),
}
ACTIVITIES = (
    ("台球局｜晚上球局来一局", "billiards", 3, 19, 6, 4, 4800, "星牌台球俱乐部", "116.4052850", "39.9145230"),
    ("桌游轰趴｜趣味欢乐局", "board-games", 4, 14, 8, 6, 6800, "朝阳桌游空间", "116.4217300", "39.9195300"),
    ("周末露营交友局", "travel", 6, 8, 12, 6, 9800, "城市绿心森林公园", "116.6862000", "39.9012000"),
)


class Command(BaseCommand):
    help = "Create or refresh deterministic local demo providers and activities."

    def handle(self, *args, **options):
        provider_categories = {}
        for sort_order, (name, slug) in enumerate(PROVIDER_CATEGORIES):
            provider_categories[slug], _ = ServiceCategory.objects.update_or_create(
                slug=slug,
                defaults={"name": name, "sort_order": sort_order, "is_active": True},
            )

        activity_categories = {}
        for sort_order, (name, slug) in enumerate(ACTIVITY_CATEGORIES):
            activity_categories[slug], _ = ActivityCategory.objects.update_or_create(
                slug=slug,
                defaults={"name": name, "sort_order": sort_order, "is_active": True},
            )

        users = []
        for index, (phone, nickname, category_slug, price, lng, lat, rating) in enumerate(PROVIDERS):
            user, _ = User.objects.update_or_create(
                phone=phone,
                defaults={
                    "nickname": nickname,
                    "gender": User.Gender.FEMALE,
                    "birth_date": date(2000 + index, 4, 15),
                    "verification_status": User.VerificationStatus.VERIFIED,
                    "account_status": User.AccountStatus.ACTIVE,
                    "is_active": True,
                },
            )
            users.append(user)
            provider, _ = ProviderProfile.objects.update_or_create(
                user=user,
                defaults={
                    "status": ProviderProfile.Status.APPROVED,
                    "bio": "认真生活，也认真陪你体验城市里的好时光。",
                    "service_city_code": "110100",
                    "service_city_name": "北京市",
                    "source_longitude": Decimal(lng),
                    "source_latitude": Decimal(lat),
                    "service_center": gcj02_to_wgs84(Decimal(lng), Decimal(lat)),
                    "max_service_radius_km": 20,
                    "rating": Decimal(rating),
                    "service_count": 32 - index * 3,
                    "order_count": 36 - index * 3,
                },
            )
            ProviderService.objects.update_or_create(
                provider=provider,
                category=provider_categories[category_slug],
                billing_type=ProviderService.BillingType.HOURLY,
                defaults={"price_amount": int(price), "is_active": True},
            )
            for weekday in range(7):
                ProviderWeeklyAvailability.objects.update_or_create(
                    provider=provider,
                    weekday=weekday,
                    starts_at="09:00",
                    ends_at="18:00",
                    defaults={"is_active": True},
                )

        demo = MULTI_SERVICE_PROVIDER
        multi_user, _ = User.objects.update_or_create(
            phone=demo["phone"],
            defaults={
                "nickname": demo["nickname"],
                "gender": User.Gender.FEMALE,
                "birth_date": date(1999, 8, 18),
                "verification_status": User.VerificationStatus.VERIFIED,
                "account_status": User.AccountStatus.ACTIVE,
                "is_active": True,
            },
        )
        if not multi_user.avatar_object_key and users[0].avatar_object_key:
            multi_user.avatar_object_key = users[0].avatar_object_key
            multi_user.save(update_fields=("avatar_object_key",))
        users.append(multi_user)
        multi_provider, _ = ProviderProfile.objects.update_or_create(
            user=multi_user,
            defaults={
                "status": ProviderProfile.Status.APPROVED,
                "bio": "喜欢旅行、桌游与城市探索，可根据你的计划灵活选择服务。",
                "service_city_code": "110100",
                "service_city_name": "北京市",
                "source_longitude": Decimal(demo["longitude"]),
                "source_latitude": Decimal(demo["latitude"]),
                "service_center": gcj02_to_wgs84(
                    Decimal(demo["longitude"]), Decimal(demo["latitude"])
                ),
                "max_service_radius_km": 30,
                "rating": Decimal("4.95"),
                "service_count": 48,
                "order_count": 53,
            },
        )
        for category_slug, billing_type, price, duration, description in demo["services"]:
            ProviderService.objects.update_or_create(
                provider=multi_provider,
                category=provider_categories[category_slug],
                billing_type=billing_type,
                defaults={
                    "price_amount": price,
                    "estimated_duration_minutes": duration,
                    "description": description,
                    "is_active": True,
                },
            )
        for weekday in range(7):
            for starts_at, ends_at in (("09:00", "12:00"), ("13:30", "20:00")):
                ProviderWeeklyAvailability.objects.update_or_create(
                    provider=multi_provider,
                    weekday=weekday,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    defaults={"is_active": True},
                )

        organizer = users[0]
        now = timezone.localtime()
        for title, category_slug, day, hour, capacity, minimum, amount, place, lng, lat in ACTIVITIES:
            starts_at = (now + timedelta(days=day)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            Activity.objects.update_or_create(
                organizer=organizer,
                title=title,
                defaults={
                    "category": activity_categories[category_slug],
                    "starts_at": starts_at,
                    "ends_at": starts_at + timedelta(hours=3),
                    "formation_deadline": starts_at - timedelta(hours=12),
                    "meeting_place_name": place,
                    "meeting_address": "北京市演示地址（非真实活动地点）",
                    "source_longitude": Decimal(lng),
                    "source_latitude": Decimal(lat),
                    "meeting_point": gcj02_to_wgs84(Decimal(lng), Decimal(lat)),
                    "capacity": capacity,
                    "min_participants": minimum,
                    "description": "用于 DAZZY 开发联调的演示活动。",
                    "participation_rules": "请准时到场，友好交流。",
                    "aa_principal_amount": amount,
                    "refund_template_version": "standard-v1",
                    "refund_rule_snapshot": {"version": "standard-v1", "demo": True},
                    "status": Activity.Status.RECRUITING,
                    "published_at": now,
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Demo data ready: {len(PROVIDERS) + 1} providers, "
                f"{len(ACTIVITIES)} activities."
            )
        )
