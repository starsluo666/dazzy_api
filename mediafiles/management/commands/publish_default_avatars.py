"""Publish the bundled avatar collection once; never change existing users."""

import hashlib
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from backoffice.access import resolve_admin_access
from backoffice.models import Organization
from mediafiles.default_avatars import DEFAULT_AVATAR_NAMES, default_avatar_keys
from mediafiles.models import MediaAsset
from mediafiles.services import upload_public_file


class Command(BaseCommand):
    help = "发布 6 张默认头像至 COS 和资源素材库，仅供之后注册的新用户随机使用。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner", type=UUID,
            help="素材归属账号 public_id：有效超级管理员或有素材管理权限的平台后台成员。",
        )
        parser.add_argument("--dry-run", action="store_true", help="仅校验和预览，不上传、不写数据库。")

    @staticmethod
    def find_owner(owner_id):
        owners = User.objects.filter(
            is_active=True, account_status=User.AccountStatus.ACTIVE,
        )
        if owner_id:
            owners = owners.filter(public_id=owner_id)
        else:
            owners = owners.filter(
                Q(is_superuser=True) | Q(
                    backoffice_memberships__is_active=True,
                    backoffice_memberships__organization__status=Organization.Status.ACTIVE,
                    backoffice_memberships__organization__organization_type=Organization.Type.PLATFORM,
                )
            )
        for owner in owners.distinct().order_by("-is_superuser", "pk").iterator():
            # Reuse the same effective membership/role as the admin asset library.
            # CLI ownership selection must not grant or change account permissions.
            try:
                access = resolve_admin_access(owner)
                access.require("asset.manage")
            except PermissionDenied:
                continue
            if access.member and access.member.organization.organization_type != Organization.Type.PLATFORM:
                continue
            return owner
        target = "指定的素材归属账号不可用" if owner_id else "未找到可用的素材归属账号"
        raise CommandError(
            f"{target}：需正常启用的超级管理员，或拥有 asset.manage 权限的平台后台成员。"
            "可用 --owner <后台用户 public_id> 指定；无需将平台后台成员提升为超级管理员。"
        )

    def handle(self, *args, **options):
        owner = self.find_owner(options["owner"])
        self.stdout.write(f"素材归属账号：{owner.public_id}")
        root = Path(settings.BASE_DIR) / "assets" / "default-avatars" / "v1"
        prepared = []
        # Validate the entire collection before any external writes.
        for name, key in zip(DEFAULT_AVATAR_NAMES, default_avatar_keys(), strict=True):
            path = root / f"{name}.webp"
            if not path.is_file():
                raise CommandError(f"缺少默认头像文件：{path}")
            content = path.read_bytes()
            with Image.open(path) as image:
                if image.format != "WEBP" or image.size != (256, 256):
                    raise CommandError(f"默认头像必须是 256×256 WebP：{path.name}")
                image.verify()
            checksum = hashlib.sha256(content).hexdigest()
            asset = MediaAsset.objects.filter(object_key=key).first()
            if asset and (
                asset.checksum_sha256 != checksum
                or asset.scope != MediaAsset.Scope.PUBLIC
                or asset.category != MediaAsset.Category.OPERATIONS_IMAGE
                or asset.status != MediaAsset.Status.UPLOADED
            ):
                raise CommandError(f"同版本头像记录不一致，禁止覆盖；请检查或发布新版本：{key}")
            prepared.append((path, key, checksum, len(content), asset))

        if options["dry_run"]:
            for path, key, _, size, asset in prepared:
                self.stdout.write(f"{'已发布' if asset else '待发布'} {path.name} ({size} bytes) → {key}")
            return

        uploaded = []
        for path, key, checksum, size, asset in prepared:
            if asset:
                continue
            # A failed upload must never make a broken URL eligible for registration.
            etag = upload_public_file(
                local_path=str(path), object_key=key, content_type="image/webp",
            )
            uploaded.append((path, key, checksum, size, etag))

        # Publish the full batch only after all uploads succeed. A retry is safe.
        with transaction.atomic():
            for path, key, checksum, size, etag in uploaded:
                MediaAsset.objects.get_or_create(
                    object_key=key,
                    defaults={
                        "owner": owner,
                        "scope": MediaAsset.Scope.PUBLIC,
                        "category": MediaAsset.Category.OPERATIONS_IMAGE,
                        "status": MediaAsset.Status.UPLOADED,
                        "original_filename": f"默认头像-{path.name}",
                        "content_type": "image/webp",
                        "size_bytes": size,
                        "etag": etag,
                        "checksum_sha256": checksum,
                        "uploaded_at": timezone.now(),
                    },
                )
        self.stdout.write(self.style.SUCCESS(
            f"默认头像已就绪：共 {len(prepared)} 张，本次上传 {len(uploaded)} 张；未修改任何已有用户。"
        ))
