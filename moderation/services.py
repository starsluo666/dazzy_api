"""Immutable submissions, fenced background work, scoped manual decisions."""

from datetime import timedelta
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import gateway, targets
from .models import ContentReview as Review, ContentReviewItem as Item


@transaction.atomic
def submit(kind, obj, *, candidate=None, force=False):
    if not settings.CONTENT_REVIEW_ENABLED and not force:
        return None
    obj = targets.target(kind, obj.pk, lock=True)
    owner, city, snapshot = targets.material(kind, obj, candidate=candidate)
    digest = targets.fingerprint(snapshot)
    previous = (
        Review.objects.select_for_update()
        .filter(kind=kind, target_id=obj.pk, is_current=True)
        .first()
    )
    if previous and previous.fingerprint == digest:
        return previous
    if (
        not force
        and Review.objects.filter(
            owner=owner, created_at__gte=timezone.now() - timedelta(days=1)
        ).count()
        >= settings.CONTENT_REVIEW_DAILY_LIMIT
    ):
        raise ValidationError("今天修改资料较频繁，请稍后再试或联系客服。")
    if previous:
        previous.is_current = False
        previous.status = "superseded"
        previous.lease_token = None
        previous.lease_until = None
        previous.revision += 1
        previous.save()
        targets.event(previous, "superseded", note="已提交新内容，旧检测结果不再生效。")
    text = "\n".join(f"{label}：{value}" for label, value in snapshot["text"].items() if value)
    automatic = settings.CONTENT_REVIEW_ENABLED and settings.TENCENT_CONTENT_REVIEW_ENABLED
    review = Review.objects.create(
        kind=kind,
        target_id=obj.pk,
        owner=owner,
        city_code=city,
        snapshot=snapshot,
        fingerprint=digest,
        search_text=text,
        base_value=getattr(obj, "nickname" if kind == "nickname" else "avatar_object_key", "")
        if kind in ("nickname", "avatar")
        else "",
        status="queued" if automatic else "manual",
        reason="" if automatic else "等待人工内容审核。",
    )
    chunks = [text[index : index + 9000] for index in range(0, len(text), 9000)]
    if len(chunks) > 10:
        raise ValidationError("本次内容过长，请精简后重新提交。")
    for position, chunk in enumerate(chunks):
        Item.objects.create(review=review, position=position, kind="text", text=chunk)
    for position, asset in enumerate(snapshot["assets"], len(chunks)):
        Item.objects.create(
            review=review, position=position, kind=asset["kind"], asset_id=asset["id"]
        )
    targets.event(review, "submitted")
    return review


def ensure_approved(kind, obj):
    _, publication = targets.published_target(kind, obj)
    if hasattr(publication, "content_hidden_review"):
        publication.refresh_from_db(fields=("content_hidden", "content_hidden_review"))
    hold = getattr(publication, "content_hidden_review", None)
    review_holds = (
        Review.objects.filter(
            kind=kind, target_id=obj.pk, is_current=True, status__in=("rejected", "withdrawn")
        )
        if kind == "review"
        else Review.objects.none()
    )
    if not settings.CONTENT_REVIEW_ENABLED and not hold and not review_holds.exists():
        return
    snapshot = targets.material(kind, obj, strict=False)[2]
    digest = targets.fingerprint(snapshot)
    if (
        hold
        and getattr(publication, "content_hidden", False)
        and Review.objects.filter(
            public_id=hold,
            fingerprint=digest,
            status__in=("rejected", "withdrawn"),
        ).exists()
    ):
        raise ValidationError("该内容已被下架，请先在原内容审核记录中核实并恢复。")
    if review_holds.filter(fingerprint=digest).exists():
        raise ValidationError("该评价已被内容审核下架或驳回，请先在内容审核记录中核实。")
    if not settings.CONTENT_REVIEW_ENABLED or (kind == "review" and obj.is_auto_generated):
        return
    targets.material(kind, obj)  # Strict asset availability before business publication.
    if not Review.objects.filter(
        kind=kind,
        target_id=obj.pk,
        is_current=True,
        fingerprint=digest,
        status="approved",
    ).exists():
        raise ValidationError("内容检测或复核尚未通过，请先在内容审核中心完成核查。")


def _current(review, obj):
    if not review.is_current or review.status == "superseded":
        return False
    if review.kind in ("nickname", "avatar"):
        field = "nickname" if review.kind == "nickname" else "avatar_object_key"
        return getattr(obj, field) in (review.base_value, review.snapshot["values"][field])
    return (
        targets.fingerprint(targets.material(review.kind, obj, strict=False)[2])
        == review.fingerprint
    )


@transaction.atomic
def decide(review_id, *, actor, action, revision, reason):
    original = Review.objects.get(pk=review_id)
    obj = targets.target(original.kind, original.target_id, lock=True)
    review = Review.objects.select_for_update().get(pk=review_id)
    if revision != review.revision or not _current(review, obj):
        raise ValidationError("内容或检测结果已变化，请刷新后重新核查。")
    if len(reason.strip()) < 5:
        raise ValidationError("请填写至少5个字的核查依据。")
    if action == "retry":
        if (
            review.status != "manual"
            or not settings.CONTENT_REVIEW_ENABLED
            or not settings.TENCENT_CONTENT_REVIEW_ENABLED
        ):
            raise ValidationError("当前内容不能重新检测，请人工复核。")
        if (
            not review.items.filter(state="error")
            .filter(~Q(kind="video") | ~Q(task_id=""))
            .exists()
        ):
            raise ValidationError("没有可安全重试的检测任务，请人工核查内容。")
        # Never discard a known video task or a completed finding.
        review.items.filter(state="error", task_id="").exclude(kind="video").update(
            state="pending", error_code="", attempts=0
        )
        review.items.filter(state="error").exclude(task_id="").update(
            state="polling", error_code="", attempts=0
        )
        review.status = "queued"
    elif action == "approve":
        if review.status not in ("queued", "processing", "manual", "rejected"):
            raise ValidationError("当前内容状态不能重复通过。")
        if review.kind not in ("nickname", "avatar"):
            targets.material(review.kind, obj)  # Recheck usable public assets before clearance.
        # A prior rejection may have removed a matching live publication.
        targets.restore(review, obj) if review.status == "rejected" and targets.public_matches(
            review, obj
        ) else targets.publish_candidate(review, obj)
        review.status = "approved"
    elif action == "reject":
        if review.status not in ("queued", "processing", "manual", "approved"):
            raise ValidationError("当前内容状态不能重复驳回。")
        if targets.public_matches(review, obj):
            targets.set_hidden(review, obj, True)
        review.status = "rejected"
    elif action == "withdraw":
        if review.status != "approved":
            raise ValidationError("当前内容不能下架。")
        targets.set_hidden(review, obj, True)
        review.status = "withdrawn"
    elif action == "restore":
        if review.status not in ("withdrawn", "rejected"):
            raise ValidationError("当前内容不能恢复。")
        if review.kind not in ("nickname", "avatar"):
            targets.material(review.kind, obj)
        targets.restore(review, obj)
        review.status = "approved"
    else:
        raise ValidationError("不支持的审核操作。")
    review.lease_token = None
    review.lease_until = None
    review.next_attempt_at = None
    review.revision += 1
    review.reason = reason.strip()
    review.reviewed_by = actor
    review.reviewed_at = timezone.now()
    review.save()
    targets.event(review, action, actor=actor, note=review.reason)
    return review


@transaction.atomic
def _claim(review_id):
    now = timezone.now()
    review = Review.objects.select_for_update().get(pk=review_id)
    if not review.is_current or review.status not in ("queued", "processing"):
        return None
    if review.lease_until and review.lease_until > now:
        return None
    if review.next_attempt_at and review.next_attempt_at > now:
        return None
    review.lease_token = uuid.uuid4()
    review.lease_until = now + timedelta(minutes=6)
    review.status = "processing"
    review.save()
    return review


def _persist_item(review, item, **values):
    with transaction.atomic():
        locked = Review.objects.select_for_update().get(pk=review.pk)
        if (
            locked.lease_token != review.lease_token
            or not locked.is_current
            or locked.status != "processing"
            or locked.lease_until <= timezone.now()
        ):
            return False
        Item.objects.filter(pk=item.pk).update(**values, updated_at=timezone.now())
        return True


def process_one(review_id):
    if not settings.CONTENT_REVIEW_ENABLED or not settings.TENCENT_CONTENT_REVIEW_ENABLED:
        return False
    if transaction.get_connection().in_atomic_block:
        raise RuntimeError("Content detection must run outside transactions")
    review = _claim(review_id)
    if review is None:
        return False
    for item in review.items.select_related("asset").order_by("position"):
        if item.state in ("pass", "review", "block", "error"):
            continue
        data_id = f"{review.public_id.hex}_{item.position}"
        try:
            if item.asset_id:
                pinned = next(
                    (a for a in review.snapshot["assets"] if a["id"] == str(item.asset_id)), None
                )
                if not pinned or targets._media([item.asset])[0] != pinned:
                    raise gateway.DetectionUnavailable("media_changed")
            if item.kind == "video" and not item.task_id:
                if item.state == "submitting":
                    raise gateway.DetectionUnavailable("video_submission_unknown")
                if not _persist_item(review, item, state="submitting", attempts=item.attempts + 1):
                    return False
                result = gateway.create_video(item.asset, data_id)
                if not _persist_item(review, item, state="polling", **result):
                    return False
                continue
            if item.attempts >= 120:
                raise gateway.DetectionUnavailable("video_query_timeout")
            if not _persist_item(review, item, attempts=item.attempts + 1):
                return False
            if item.kind == "text":
                result = gateway.detect_text(item.text, data_id)
            elif item.kind == "image":
                result = gateway.detect_image(item.asset, data_id)
            else:
                result = gateway.query_video(item.task_id, data_id)
            if result is not None:
                _persist_item(
                    review, item, state=result["suggestion"].lower(), error_code="", **result
                )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, gateway.DetectionUnavailable)
                else "detection_unavailable"
            )
            _persist_item(review, item, state="error", error_code=code)
    _finish(review)
    return True


@transaction.atomic
def _finish(original):
    try:
        obj = targets.target(original.kind, original.target_id, lock=True)
    except ValidationError:
        obj = None
    review = Review.objects.select_for_update().get(pk=original.pk)
    if (
        review.lease_token != original.lease_token
        or not review.is_current
        or review.status != "processing"
        or review.lease_until <= timezone.now()
    ):
        return
    if obj is None or not _current(review, obj):
        review.status = "superseded"
        review.is_current = False
        review.reason = "原内容已删除或变更，保留本次审核记录。"
    else:
        states = set(review.items.values_list("state", flat=True))
        if "block" in states:
            review.status, review.machine_suggestion = "rejected", "Block"
            review.reason = "内容未通过检测，请修改后重新提交；如有异议可联系客服复核。"
            if targets.public_matches(review, obj):
                targets.set_hidden(review, obj, True)
        elif states.intersection(("review", "error")):
            review.status, review.machine_suggestion = "manual", "Review"
            review.reason = "检测结果需要人工核查。"
        elif states.difference(("pass",)):
            review.status = "queued"
            review.next_attempt_at = timezone.now() + timedelta(seconds=30)
        else:
            try:
                targets.publish_candidate(review, obj)
            except ValidationError:
                review.status, review.machine_suggestion = "manual", "Review"
                review.reason = "当前资料状态需要人工核查。"
            else:
                review.status, review.machine_suggestion = "approved", "Pass"
                review.reason = "内容检测通过。"
    review.lease_token = None
    review.lease_until = None
    review.revision += 1
    review.save()
    targets.event(review, "machine_result", note=review.reason)


def pending_ids(limit=20):
    now = timezone.now()
    return list(
        Review.objects.filter(is_current=True, status__in=("queued", "processing"))
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("updated_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
