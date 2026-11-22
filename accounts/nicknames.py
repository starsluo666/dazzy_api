"""Nickname format and reusable local matching helpers."""
from functools import lru_cache
from pathlib import Path
import unicodedata

from django.conf import settings
from rest_framework.exceptions import ValidationError


def matching_text(value):
    # Ignore full-width forms, separators and zero-width tricks when matching.
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in normalized if unicodedata.category(ch)[0] in ("L", "N"))


@lru_cache(maxsize=1)
def builtin_words():
    path = Path(__file__).with_name("data") / "nickname_blocked_words.txt"
    return tuple(
        matching_text(line) for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    )


def validate_nickname_format(value):
    nickname = value.strip()
    if not any(not ch.isspace() and unicodedata.category(ch)[0] != "C" for ch in nickname):
        raise ValidationError("昵称不能为空。")
    return nickname


def nickname_rule_matches(value):
    normalized = matching_text(value)
    extra = (matching_text(word) for word in settings.NICKNAME_EXTRA_BLOCKED_WORDS)
    return any(word and word in normalized for word in (*builtin_words(), *extra))


def validate_nickname(value):
    nickname = validate_nickname_format(value)
    if nickname_rule_matches(nickname):
        raise ValidationError("昵称包含不合适的内容，请更换昵称。")
    return nickname
