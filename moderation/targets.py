"""Allowlisted public content. Identity, selfies and order evidence never enter CMS."""

import hashlib
import json
from decimal import Decimal

from django.apps import apps
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from mediafiles.models import MediaAsset
from .models import ContentReview as Review

MODELS = {
    "nickname": "accounts.User",
    "avatar": "accounts.User",
    "provider": "providers.ProviderProfile",
    "profile_revision": "providers.ProviderProfileRevision",
    "service": "providers.ProviderService",
    "service_revision": "providers.ProviderServiceRevision",
    "activity": "activities.Activity",
    "review": "orders.ProviderOrderReview",
}
PUBLIC_CATEGORIES = {
    "avatar",
    "provider_photo",
    "provider_video",
    "activity_cover",
    "review_image",
    "operations_image",
}


def target(kind, pk, *, lock=False):
    if kind not in MODELS:
        raise ValidationError("不支持的内容类型。")
    qs = apps.get_model(MODELS[kind]).objects.all()
    if lock:
        if kind in ("profile_revision", "service_revision", "service"):
            provider_id = qs.filter(pk=pk).values_list("provider_id", flat=True).first()
            apps.get_model("providers.ProviderProfile").objects.select_for_update().filter(
                pk=provider_id
            ).first()
        elif kind == "review":
            order_id = qs.filter(pk=pk).values_list("order_id", flat=True).first()
            apps.get_model("orders.ProviderOrder").objects.select_for_update().filter(
                pk=order_id
            ).first()
        qs = qs.select_for_update()
    obj = qs.filter(pk=pk).first()
    if obj is None:
        raise ValidationError("原内容不存在，请刷新后重试。")
    return obj


def fingerprint(snapshot):
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _media(assets, *, strict=True):
    result = []
    for asset in assets:
        if strict and (
            asset.scope != "public"
            or asset.category not in PUBLIC_CATEGORIES
            or asset.status not in ("uploaded", "verified")
        ):
            raise ValidationError("公开展示素材不可用，请更换后重新提交。")
        result.append(
            {
                "id": str(asset.pk),
                "key": asset.object_key,
                "etag": asset.etag,
                "checksum": asset.checksum_sha256,
                "kind": "video" if asset.category == "provider_video" else "image",
            }
        )
    if len(result) > 12:
        raise ValidationError("单次送审素材过多。")
    return result


def material(kind, obj, *, candidate=None, strict=True):
    assets, city, values = [], "", {}
    if kind in ("nickname", "avatar"):
        owner = obj
        if kind == "nickname":
            value = obj.nickname if candidate is None else str(candidate).strip()
            if not 1 <= len(value) <= 30:
                raise ValidationError("昵称须为1至30个字。")
            text, values = {"昵称": value}, {"nickname": value}
        else:
            if candidate is None:
                asset = MediaAsset.objects.filter(object_key=obj.avatar_object_key).first()
            else:
                asset = MediaAsset.objects.filter(
                    pk=candidate, owner=obj, category="avatar"
                ).first()
                if asset is None:
                    raise ValidationError("头像不存在或不属于当前账号。")
            assets = [asset] if asset else []
            text, values = (
                {},
                {"avatar_object_key": asset.object_key if asset else obj.avatar_object_key},
            )
    elif kind in ("provider", "profile_revision"):
        provider = obj if kind == "provider" else obj.provider
        owner, city = provider.user, obj.service_city_code
        text = {"达人名称": obj.display_name, "个人介绍": obj.bio}
        values = {
            field: getattr(obj, field)
            for field in (
                "display_name",
                "bio",
                "service_city_code",
                "service_city_name",
                "max_service_radius_km",
            )
        }
        assets = [
            item.asset for item in obj.gallery_items.select_related("asset").order_by("position")
        ]
        if not assets and obj.lifestyle_photo_id:
            assets = [obj.lifestyle_photo]
    elif kind in ("service", "service_revision"):
        owner, city = obj.provider.user, obj.provider.service_city_code
        text = {"服务说明": obj.description}
        values = {
            field: getattr(obj, field)
            for field in (
                "category_id",
                "description",
                "billing_type",
                "price_amount",
                "estimated_duration_minutes",
            )
        }
    elif kind == "activity":
        owner, city = obj.organizer, obj.city_code
        text = {
            "标题": obj.title,
            "活动介绍": obj.description,
            "参与规则": obj.participation_rules,
            "集合地点": obj.meeting_place_name,
            "集合地址": obj.meeting_address,
        }
        assets = [obj.cover] if obj.cover_id else []
    else:
        owner, city = obj.customer, obj.provider.service_city_code
        text = {"评价": obj.content}
        assets = list(obj.images.order_by("pk"))
        values = {"rating": obj.rating, "is_anonymous": obj.is_anonymous}
    return (
        owner,
        city,
        {
            "text": text,
            "values": {k: str(v) if isinstance(v, Decimal) else v for k, v in values.items()},
            "assets": _media(assets, strict=strict),
            "owner_id": owner.pk,
            "city_code": city,
        },
    )


def published_target(kind, obj):
    if kind == "profile_revision":
        return "provider", obj.provider
    if kind == "service_revision":
        return ("service", obj.service) if obj.service_id else ("service", None)
    return kind, obj


def public_matches(review, obj):
    kind, published = published_target(review.kind, obj)
    return (
        published is not None
        and fingerprint(material(kind, published, strict=False)[2]) == review.fingerprint
    )


def _order_review_result(review, obj, *, approved, actor=None, restore_visibility=False):
    from notifications.models import UserNotification
    from notifications.services import create_order_notification
    from orders.services import refresh_provider_review_metrics

    status = obj.AuditStatus.APPROVED if approved else obj.AuditStatus.REJECTED
    if obj.audit_status == status:
        return
    obj.audit_status = status
    obj.audit_rejection_reason = "" if approved else "评价内容不符合公开展示要求，如有异议请联系客服。"
    obj.audited_by = actor
    obj.audited_at = timezone.now()
    obj.is_visible = approved and (obj.is_visible or restore_visibility)
    obj.save(update_fields=(
        "audit_status", "audit_rejection_reason", "audited_by", "audited_at", "is_visible", "updated_at",
    ))
    refresh_provider_review_metrics(obj.provider)
    create_order_notification(
        order=obj.order,
        event_type=UserNotification.EventType.ORDER_REVIEW_RESULT,
        title="评价审核通过" if approved else "评价审核未通过",
        content=(
            "您的订单评价已审核通过并公开展示。" if obj.is_visible else "您的订单评价已审核通过。"
        ) if approved else obj.audit_rejection_reason,
        dedupe_suffix=f"content:{review.public_id}:{review.revision}",
    )


def publish_candidate(review, obj, *, actor=None):
    """Publish user avatars/nicknames and order reviews; other approvals remain separate."""
    if review.kind == Review.Kind.REVIEW:
        restore = actor is not None and review.status in (Review.Status.REJECTED, Review.Status.WITHDRAWN)
        if obj.audit_status == obj.AuditStatus.APPROVED or (
            obj.audit_status == obj.AuditStatus.REJECTED and not restore
        ):
            return
        material(review.kind, obj)  # Revalidate public assets before publication.
        _order_review_result(review, obj, approved=True, actor=actor, restore_visibility=restore)
        return
    if review.kind in (Review.Kind.NICKNAME, Review.Kind.AVATAR):
        for pinned in review.snapshot["assets"]:
            asset = MediaAsset.objects.filter(pk=pinned["id"]).first()
            if not asset or _media([asset])[0] != pinned:
                raise ValidationError("素材已变更或不可用，请重新提交。")
        if not obj.is_active or obj.account_status != "active":
            raise ValidationError("账号当前不可发布新资料。")
        field = "nickname" if review.kind == "nickname" else "avatar_object_key"
        value = review.snapshot["values"][field]
        if review.kind == "nickname":
            from accounts.nicknames import validate_nickname_format
            value = validate_nickname_format(value)
        setattr(obj, field, value)
        obj.save(update_fields=(field,))


def set_hidden(review, obj, hidden):
    """Remove a matching publication without changing orders or payment state."""
    kind, published = published_target(review.kind, obj)
    if published is None or not public_matches(review, obj):
        raise ValidationError("当前公开内容已变化，请对最新内容重新核查。")
    if kind in ("nickname", "avatar"):
        if hidden:
            if kind == "nickname":
                published.nickname = f"用户{published.phone[-4:]}"
                published.save(update_fields=("nickname",))
            else:
                from mediafiles.default_avatars import choose_default_avatar

                published.avatar_object_key = choose_default_avatar()
                published.save(update_fields=("avatar_object_key",))
            review.base_value = getattr(
                published, "nickname" if kind == "nickname" else "avatar_object_key"
            )
        else:
            publish_candidate(review, published)
    elif kind == "review":
        published.is_visible = not hidden
        published.save(update_fields=("is_visible", "updated_at"))
        from orders.services import refresh_provider_review_metrics

        refresh_provider_review_metrics(published.provider)
    else:
        if published.content_hidden and published.content_hidden_review not in (
            None,
            review.public_id,
        ):
            if hidden:
                return  # Another review already owns the hold; never steal its restoration authority.
            raise ValidationError("内容由其他审核记录下架，请在原下架记录中核实恢复。")
        published.content_hidden = hidden
        published.content_hidden_review = review.public_id if hidden else None
        published.save(update_fields=("content_hidden", "content_hidden_review", "updated_at"))


def reject_candidate(review, obj, *, actor=None):
    if not public_matches(review, obj):
        return
    if review.kind == Review.Kind.REVIEW:
        _order_review_result(review, obj, approved=False, actor=actor)
    else:
        set_hidden(review, obj, True)


def restore(review, obj, *, actor=None):
    if review.kind == Review.Kind.REVIEW and obj.audit_status != obj.AuditStatus.APPROVED:
        publish_candidate(review, obj, actor=actor)
        return
    if review.kind in ("nickname", "avatar"):
        publish_candidate(review, obj)
    else:
        set_hidden(review, obj, False)


def event(review, action, *, actor=None, note=""):
    review.events.create(action=action, revision=review.revision, actor=actor, note=note[:500])


def user_status(user):
    return [
        {
            "kind": row.kind,
            "status": row.status,
            "label": row.get_status_display(),
            "reason": "内容未通过审核，请修改后重新提交。" if row.status in ("rejected", "withdrawn") else "",
            "submitted_at": row.created_at,
        }
        for row in Review.objects.filter(
            owner=user, kind__in=("avatar", "nickname"), is_current=True
        )
    ]
