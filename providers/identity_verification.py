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


def fingerprint(profile, *, front_only=False):
    values = [profile.application_real_name.strip(), profile.identity_real_name.strip(),
              profile.identity_number_digest]
    fields = ("identity_front_photo",) if front_only else (
        "identity_front_photo", "identity_back_photo", "identity_face_photo",
    )
    for field in fields:
        asset = getattr(profile, field)
        values.append([str(asset.pk), asset.object_key, asset.etag, asset.status] if asset else None)
        if front_only and asset:
            values.append([str(asset.owner_id), asset.scope, asset.category])
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def matches_check(profile, check):
    return bool(check and check.fingerprint == fingerprint(
        profile, front_only=check.material_scope == "front",
    ))


def fresh_check(check):
    return bool(check and check.completed_at and check.completed_at > timezone.now() - timedelta(hours=24))


def active_check(check):
    return bool(check and check.status == "processing" and check.created_at > timezone.now() - LEASE)


def ensure_not_checking(profile):
    if active_check(profile.identity_checks.first()):
        raise ValidationError({"detail": "身份证正在验真，请稍后刷新结果，不要重复提交或修改材料。"})


def verification_summary(profile, *, admin=False):
    check = profile.identity_checks.first()
    current = matches_check(profile, check)
    state = check.status if current else "not_checked"
    message = check.message if current else "尚未对当前材料进行身份证验真。"
    if state == "processing" and not active_check(check):
        state, message = "error", "上次验真未完成，请重新提交；仍有问题请联系客服。"
    if (state == "passed" and not fresh_check(check)
            and profile.identity_status in ("unverified", "rejected")):
        state, message = "not_checked", "验真结果已过期，请重新验真后提交认证。"
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
    if not check or check.status != "passed" or not matches_check(profile, check):
        raise ValidationError("当前认证材料尚未通过身份证验真，请驳回后由达人重新提交。")
    validate_assets(profile)


def validate_assets(profile, *, front_only=False):
    fields = ("identity_front_photo",) if front_only else (
        "identity_front_photo", "identity_back_photo", "identity_face_photo",
    )
    for name in fields:
        asset = getattr(profile, name)
        if (not asset or asset.owner_id != profile.user_id
                or asset.scope != MediaAsset.Scope.PRIVATE
                or asset.category != MediaAsset.Category.IDENTITY
                or asset.status != MediaAsset.Status.UPLOADED):
            raise ValidationError({name: "认证材料不存在、已失效或无权使用，请重新上传。"})


def validate_account(profile):
    if (profile.status != ProviderProfile.Status.APPROVED or not profile.user.is_active
            or profile.user.account_status != "active"):
        raise ValidationError({"detail": "当前账户状态不能提交实名认证。"})


@transaction.atomic
def submit_verified_identity(*, provider):
    """New clients submit for manual review only; this action never calls Tencent."""
    from .services import mark_identity_submitted, validate_identity_submission
    profile = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    validate_account(profile)
    validate_identity_submission(profile, auto_recognize=True)
    validate_assets(profile)
    ensure_not_checking(profile)
    check = profile.identity_checks.first()
    if (not profile.identity_verification_required or not check or check.status != "passed"
            or not matches_check(profile, check) or not fresh_check(check)):
        raise ValidationError({"detail": "请先完成当前身份证人像面的验真，再提交认证。"})
    return mark_identity_submitted(profile)


def verify_identity(*, provider, consent_version, identity_front_photo_id):
    return _run_identity_check(
        provider=provider, consent_version=consent_version, recognition_mode="photo",
        front_only=True, expected_front_id=identity_front_photo_id,
    )


def submit_identity(*, provider, consent_version, recognition_mode="", verification_mode="on_submit"):
    if verification_mode == "preverified":
        return submit_verified_identity(provider=provider)
    return _run_identity_check(
        provider=provider, consent_version=consent_version, recognition_mode=recognition_mode,
    )


def _run_identity_check(*, provider, consent_version, recognition_mode="", front_only=False,
                        expected_front_id=None):
    from .services import mark_identity_submitted, validate_identity_submission

    with transaction.atomic():
        profile = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
        validate_account(profile)
        needs_check = (recognition_mode == "photo" or settings.TENCENT_FACEID_ENABLED
                       or profile.identity_verification_required)
        if front_only:
            if profile.identity_status not in ("unverified", "rejected"):
                raise ValidationError({"detail": "当前实名认证状态不可重复验真。"})
            if not profile.application_real_name.strip():
                raise ValidationError({"detail": "入驻申请姓名缺失，请联系客服核实补充。"})
            if profile.identity_front_photo_id != expected_front_id:
                raise ValidationError({"detail": "身份证人像面已变化，请刷新后重试。"})
        else:
            validate_identity_submission(profile, auto_recognize=needs_check)
        validate_assets(profile, front_only=front_only)
        ensure_not_checking(profile)
        # A started verification cannot be bypassed by toggling the feature off mid-flow.
        if not needs_check:
            return mark_identity_submitted(profile)
        if not settings.TENCENT_FACEID_ENABLED or not all(credentials()):
            raise IdentityVerificationUnavailable("身份证自动识别与验真暂未开放，请先保存照片，稍后重试或联系客服。")
        if consent_version != CONSENT_VERSION:
            raise ValidationError({"consent_version": "请阅读并同意腾讯云身份证验真授权后再提交。"})
        snapshot = fingerprint(profile, front_only=front_only)
        latest = profile.identity_checks.first()
        if latest and latest.status == "passed" and matches_check(profile, latest) and fresh_check(latest):
            return profile if front_only else mark_identity_submitted(profile)
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
            material_scope="front" if front_only else "all",
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
            and matches_check(profile, attempt)
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
                attempt.fingerprint = fingerprint(profile, front_only=front_only)
        attempt.status, attempt.result_code = result.status, result.code
        attempt.message, attempt.request_id = result.message, result.request_id
        attempt.similarity, attempt.completed_at = result.similarity, timezone.now()
        attempt.save(update_fields=("status", "result_code", "message", "request_id",
                                    "similarity", "completed_at", "fingerprint"))
        if result.status == "passed" and not front_only:
            profile = mark_identity_submitted(profile)
    # Raise after committing: retain failed receipts and enforce rate limits on retries.
    if result.status == "error":
        raise IdentityVerificationUnavailable(result.message)
    if result.status != "passed":
        raise ValidationError({"identity_front_photo": result.message})
    return profile
