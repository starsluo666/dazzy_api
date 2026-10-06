"""Versioned, shared avatars. Registration never uploads files or calls COS."""

from pathlib import PurePosixPath
from secrets import choice

from django.conf import settings

from .models import MediaAsset


DEFAULT_AVATAR_NAMES = ("cat", "dog", "bear", "rabbit", "panda", "otter")


def default_avatar_keys() -> tuple[str, ...]:
    root = PurePosixPath(settings.COS_PUBLIC_PREFIX) / "defaults" / "avatars" / "v1"
    return tuple(str(root / f"{name}.webp") for name in DEFAULT_AVATAR_NAMES)


def is_default_avatar(object_key: str) -> bool:
    return object_key in default_avatar_keys()


def choose_default_avatar(*, using: str = "default") -> str:
    # Only select files successfully published by publish_default_avatars.
    # Before publication, keep the existing client-side placeholder.
    available = list(
        MediaAsset.objects.using(using).filter(
            object_key__in=default_avatar_keys(),
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.OPERATIONS_IMAGE,
            status=MediaAsset.Status.UPLOADED,
        ).values_list("object_key", flat=True)
    )
    return choice(available) if available else ""
