import hashlib
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.models import User
from activities.models import Activity
from mediafiles.models import MediaAsset
from mediafiles.services import upload_public_file


AVATARS = {
    "13810000001": "xiaoxiao.webp",
    "13810000002": "xiaoyu.webp",
    "13810000003": "tiantian.webp",
    "13810000004": "keke.webp",
}
AVATAR_ALIASES = {"13810000005": "13810000001"}

ACTIVITY_COVERS = {
    "台球局｜晚上球局来一局": "billiards.webp",
    "桌游轰趴｜趣味欢乐局": "board-games.webp",
    "周末露营交友局": "camping.webp",
}

HOME_CARD_ASSETS = {
    "provider-companion": "provider-companion.webp",
    "group-activity": "group-activity.webp",
}


class Command(BaseCommand):
    help = "Upload generated demo media to COS and bind it to demo records."

    def handle(self, *args, **options):
        media_root = Path(settings.BASE_DIR) / "assets" / "demo-media"
        missing = [
            path
            for path in [
                *(media_root / "avatars" / name for name in AVATARS.values()),
                *(media_root / "activity-covers" / name for name in ACTIVITY_COVERS.values()),
                *(media_root / "home-cards" / name for name in HOME_CARD_ASSETS.values()),
            ]
            if not path.is_file()
        ]
        if missing:
            raise CommandError(f"Missing demo media: {', '.join(map(str, missing))}")

        for phone, filename in AVATARS.items():
            user = User.objects.get(phone=phone)
            local_path = media_root / "avatars" / filename
            object_key = self._key("avatars", filename)
            self._upload_and_record(
                owner=user,
                local_path=local_path,
                object_key=object_key,
                category=MediaAsset.Category.AVATAR,
            )
            user.avatar_object_key = object_key
            user.save(update_fields=("avatar_object_key",))

        for target_phone, source_phone in AVATAR_ALIASES.items():
            target = User.objects.get(phone=target_phone)
            target.avatar_object_key = User.objects.get(phone=source_phone).avatar_object_key
            target.save(update_fields=("avatar_object_key",))

        for title, filename in ACTIVITY_COVERS.items():
            activity = Activity.objects.get(title=title)
            local_path = media_root / "activity-covers" / filename
            object_key = self._key("activity-covers", filename)
            asset = self._upload_and_record(
                owner=activity.organizer,
                local_path=local_path,
                object_key=object_key,
                category=MediaAsset.Category.ACTIVITY_COVER,
            )
            activity.cover = asset
            activity.save(update_fields=("cover", "updated_at"))

        asset_owner = User.objects.get(phone="13810000001")
        for filename in HOME_CARD_ASSETS.values():
            self._upload_and_record(
                owner=asset_owner,
                local_path=media_root / "home-cards" / filename,
                object_key=self._key("home-cards", filename),
                category=MediaAsset.Category.OTHER,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Demo media ready: {len(AVATARS)} avatars, "
                f"{len(ACTIVITY_COVERS)} activity covers, "
                f"{len(HOME_CARD_ASSETS)} home card assets."
            )
        )

    @staticmethod
    def _key(folder: str, filename: str) -> str:
        return str(PurePosixPath(settings.COS_PUBLIC_PREFIX) / "demo" / folder / filename)

    @staticmethod
    def _upload_and_record(*, owner, local_path, object_key, category):
        content = local_path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        etag = upload_public_file(
            local_path=str(local_path),
            object_key=object_key,
            content_type="image/webp",
        )
        asset, _ = MediaAsset.objects.update_or_create(
            object_key=object_key,
            defaults={
                "owner": owner,
                "scope": MediaAsset.Scope.PUBLIC,
                "category": category,
                "status": MediaAsset.Status.VERIFIED,
                "original_filename": local_path.name,
                "content_type": "image/webp",
                "size_bytes": len(content),
                "etag": etag,
                "checksum_sha256": checksum,
                "uploaded_at": timezone.now(),
            },
        )
        return asset
