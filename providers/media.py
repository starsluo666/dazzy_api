"""Ordered public galleries and their review snapshots (the cover stays compatible)."""

from rest_framework.exceptions import ValidationError

from mediafiles.models import MediaAsset
from mediafiles.services import build_media_url

from .models import ProviderProfileMedia, ProviderProfileRevisionMedia

MAX_MEDIA = 9
MAX_VIDEOS = 3


def gallery_assets(profile_or_revision):
    items = profile_or_revision.gallery_items.all()
    if "gallery_items" not in getattr(profile_or_revision, "_prefetched_objects_cache", {}):
        items = items.select_related("asset")
    assets = [item.asset for item in items]
    if not assets and profile_or_revision.lifestyle_photo_id:
        assets = [profile_or_revision.lifestyle_photo]
    return assets


def gallery_payload(profile_or_revision):
    return [
        {
            "id": str(asset.pk),
            "type": "video" if asset.category == MediaAsset.Category.PROVIDER_VIDEO else "image",
            "url": build_media_url(asset.object_key),
        }
        for asset in gallery_assets(profile_or_revision)
    ]


def resolve_gallery(provider, data):
    if "media_ids" in data:
        ids = data["media_ids"]
        available = MediaAsset.objects.in_bulk(ids)
        assets = [available.get(asset_id) for asset_id in ids]
    else:
        assets = gallery_assets(provider)
        if "lifestyle_photo" in data:
            cover = data["lifestyle_photo"]
            assets = (
                ([cover] + [asset for asset in assets[1:] if asset.pk != cover.pk]) if cover else []
            )
    if not assets or len(assets) > MAX_MEDIA:
        raise ValidationError({"media_ids": "请上传1至9个展示素材，第一张必须是照片。"})
    if any(
        asset is None
        or asset.owner_id != provider.user_id
        or asset.scope != MediaAsset.Scope.PUBLIC
        or asset.status != MediaAsset.Status.UPLOADED
        or asset.category
        not in (MediaAsset.Category.PROVIDER_PHOTO, MediaAsset.Category.PROVIDER_VIDEO)
        for asset in assets
    ):
        raise ValidationError({"media_ids": "素材不存在、尚未上传成功或无权使用。"})
    if len({asset.pk for asset in assets}) != len(assets):
        raise ValidationError({"media_ids": "展示素材不能重复。"})
    if assets[0].category != MediaAsset.Category.PROVIDER_PHOTO:
        raise ValidationError({"media_ids": "第一张必须是照片，将作为列表封面。"})
    if sum(asset.category == MediaAsset.Category.PROVIDER_VIDEO for asset in assets) > MAX_VIDEOS:
        raise ValidationError({"media_ids": "最多上传3个视频。"})
    return assets


def save_revision_gallery(revision, assets):
    ProviderProfileRevisionMedia.objects.bulk_create(
        [
            ProviderProfileRevisionMedia(revision=revision, asset=asset, position=index)
            for index, asset in enumerate(assets)
        ]
    )


def apply_revision_gallery(profile, revision):
    # Called inside the same transaction as approval, never on rejection.
    assets = resolve_gallery(
        profile, {"media_ids": [asset.pk for asset in gallery_assets(revision)]}
    )
    profile.gallery_items.all().delete()
    ProviderProfileMedia.objects.bulk_create(
        [
            ProviderProfileMedia(provider=profile, asset=asset, position=index)
            for index, asset in enumerate(assets)
        ]
    )
