"""Submission gate around Tencent verification, without holding DB locks during I/O."""
from datetime import timedelta
import hashlib
import json

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from mediafiles.models import MediaAsset
from .models import ProviderIdentityCheck, ProviderProfile
from .tencent_identity import CheckResult, check_identity_photo, credentials

CONSENT_VERSION = "tencent-id-card-photo-v2"
CONSENT_NOTICE = (
    "为验证本人身份证真实性，我同意平台将已上传的身份证人像面照片提交腾讯云，"
    "由其识别姓名、证件号并与权威库证件照进行比对；验真通过后自动记录实名信息，"
    "并与入驻申请姓名核对，无需手动填写姓名或证件号。"
    "本次不进行活体检测，国徽面有效期与本人自拍仅由平台客服人工审核，不提交该验真接口。"
    "认证照片保持私有存储，公开页面不展示证件信息。"
)
LEASE = timedelta(minutes=2)


class IdentityVerificationUnavailable(APIException):
    status_code = 503
    default_detail = "身份证验真服务暂不可用，请稍后重试或联系客服。"
    default_code = "identity_verification_unavailable"


def fingerprint(profile):
    values = [profile.application_real_name.strip(), profile.identity_real_name.strip(),
              profile.identity_number_digest]
    for field in ("identity_front_photo", "identity_back_photo", "identity_face_photo"):
        asset = getattr(profile, field)
        values.append([str(asset.pk), asset.object_key, asset.etag, asset.status] if asset else None)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def active_check(check):
    return bool(check and check.status == "processing" and check.created_at > timezone.now() - LEASE)


def ensure_not_checking(profile):
    if active_check(profile.identity_checks.first()):
        raise ValidationError({"detail": "身份证正在验真，请稍后刷新结果，不要重复提交或修改材料。"})


def verification_summary(profile, *, admin=False):
    check = profile.identity_checks.first()
    current = bool(check and check.fingerprint == fingerprint(profile))
    state = check.status if current else "not_checked"
    message = check.message if current else "尚未对当前材料进行身份证验真。"
    if state == "processing" and not active_check(check):
        state, message = "error", "上次验真未完成，请重新提交；仍有问题请联系客服。"
    labels = {"not_checked": "未验真", "processing": "验真中", "passed": "身份证验真通过",
              "failed": "身份证验真未通过", "error": "验真未完成"}
    data = {
        "enabled": settings.TENCENT_FACEID_ENABLED,
        "required": profile.identity_verification_required,
        "consent_version": CONSENT_VERSION, "notice": CONSENT_NOTICE,
        "status": state, "status_label": labels[state], "message": message,
        "checked_at": check.completed_at if current else None,
    }
    if admin:
        data.update(request_id=check.request_id if current else "",
                    result_code=check.result_code if current else "",
                    similarity=str(check.similarity) if current and check.similarity is not None else None)
    return data


def require_verified_check(profile):
    """Both standalone identity approval and combined onboarding must use this gate."""
    if not profile.identity_verification_required:
        return  # Historical manual submissions are grandfathered, not labeled machine-verified.
    check = profile.identity_checks.first()
    if not check or check.status != "passed" or check.fingerprint != fingerprint(profile):
        raise ValidationError("当前认证材料尚未通过身份证验真，请驳回后由达人重新提交。")


def validate_assets(profile):
    for name in ("identity_front_photo", "identity_back_photo", "identity_face_photo"):
        asset = getattr(profile, name)
        if (not asset or asset.owner_id != profile.user_id
                or asset.scope != MediaAsset.Scope.PRIVATE
                or asset.category != MediaAsset.Category.IDENTITY
                or asset.status != MediaAsset.Status.UPLOADED):
            raise ValidationError({name: "认证材料不存在、已失效或无权使用，请重新上传。"})


def submit_identity(*, provider, consent_version, recognition_mode=""):
    from .services import mark_identity_submitted, validate_identity_submission

    with transaction.atomic():
        profile = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
        if (profile.status != ProviderProfile.Status.APPROVED or not profile.user.is_active
                or profile.user.account_status != "active"):
            raise ValidationError({"detail": "当前账户状态不能提交实名认证。"})
        needs_check = (recognition_mode == "photo" or settings.TENCENT_FACEID_ENABLED
                       or profile.identity_verification_required)
        validate_identity_submission(profile, auto_recognize=needs_check)
        validate_assets(profile)
        ensure_not_checking(profile)
        # A started verification cannot be bypassed by toggling the feature off mid-flow.
        if not needs_check:
            return mark_identity_submitted(profile)
        if not settings.TENCENT_FACEID_ENABLED or not all(credentials()):
            raise IdentityVerificationUnavailable("身份证自动识别与验真暂未开放，请先保存照片，稍后重试或联系客服。")
        if consent_version != CONSENT_VERSION:
            raise ValidationError({"consent_version": "请阅读并同意腾讯云身份证验真授权后再提交。"})
        snapshot = fingerprint(profile)
        latest = profile.identity_checks.first()
        if (latest and latest.status == "passed" and latest.fingerprint == snapshot
                and latest.completed_at and latest.completed_at > timezone.now() - timedelta(hours=24)):
            return mark_identity_submitted(profile)
        now = timezone.now()
        if latest and latest.created_at > now - timedelta(seconds=30):
            raise ValidationError({"detail": "提交过于频繁，请30秒后再试。"})
        count = profile.identity_checks.filter(created_at__gte=now - timedelta(days=1)).count()
        if count >= settings.TENCENT_FACEID_DAILY_LIMIT:
            raise ValidationError({"detail": "近24小时验真次数已达上限，请稍后再试或联系客服。"})
        if latest and latest.status == "processing":
            latest.status, latest.result_code = "error", "ExpiredAttempt"
            latest.message, latest.completed_at = "验真请求已超时，未采纳其结果。", now
            latest.save(update_fields=("status", "result_code", "message", "completed_at"))
        profile.identity_verification_required = True
        profile.save(update_fields=("identity_verification_required", "updated_at"))
        attempt = ProviderIdentityCheck.objects.create(
            provider=profile, fingerprint=snapshot, consent_version=consent_version,
        )

    # All remote work is outside the transaction. SDK timeout does not release stale attempts
    # into the review queue. The next explicit submission can supersede an expired attempt.
    try:
        result = check_identity_photo(asset=profile.identity_front_photo,
                                      application_real_name=profile.application_real_name)
    except Exception:
        result = CheckResult("error", "UnexpectedError", "身份证验真未完成，请稍后重试或联系客服。")

    with transaction.atomic():
        profile = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
        attempt.refresh_from_db()
        latest = profile.identity_checks.first()
        valid = (
            latest.pk == attempt.pk and active_check(attempt)
            and attempt.fingerprint == fingerprint(profile)
            and profile.status == ProviderProfile.Status.APPROVED and profile.user.is_active
            and profile.user.account_status == "active"
            and profile.identity_status in (ProviderProfile.IdentityStatus.UNVERIFIED,
                                             ProviderProfile.IdentityStatus.REJECTED)
        )
        if not valid:
            result = CheckResult("error", "StaleAttempt", "认证材料或账户状态已变化，请刷新后重新提交。")
        if result.status == "passed":
            if (not result.number_digest or not result.number_masked
                    or not result.real_name or result.real_name != profile.application_real_name.strip()):
                result = CheckResult("error", "InvalidResponse", "身份证识别结果不完整，请稍后重试或联系客服。")
            else:
                # Only a successful, current channel result can write recognized identity.
                profile.identity_real_name = result.real_name
                profile.identity_number_digest = result.number_digest
                profile.identity_number_masked = result.number_masked
                profile.save(update_fields=("identity_real_name", "identity_number_digest",
                                            "identity_number_masked", "updated_at"))
                attempt.fingerprint = fingerprint(profile)
        attempt.status, attempt.result_code = result.status, result.code
        attempt.message, attempt.request_id = result.message, result.request_id
        attempt.similarity, attempt.completed_at = result.similarity, timezone.now()
        attempt.save(update_fields=("status", "result_code", "message", "request_id",
                                    "similarity", "completed_at", "fingerprint"))
        if result.status == "passed":
            profile = mark_identity_submitted(profile)
    # Raise after committing: retain failed receipts and enforce rate limits on retries.
    if result.status == "error":
        raise IdentityVerificationUnavailable(result.message)
    if result.status != "passed":
        raise ValidationError({"identity_front_photo": result.message})
    return profile
