"""Tencent CheckIdCardInformation adapter. Sensitive payloads live in memory only."""
import base64
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO
import logging
import re
import warnings

from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError
from qcloud_cos import CosConfig, CosS3Client
from tencentcloud.common.credential import Credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.retry import NoopRetryer
from tencentcloud.faceid.v20180301.faceid_client import FaceidClient
from tencentcloud.faceid.v20180301.models import CheckIdCardInformationRequest

# The SDK logs full request/response bodies at DEBUG (including ID numbers and photos).
logging.getLogger("tencentcloud_sdk_common").disabled = True


@dataclass(frozen=True)
class CheckResult:
    status: str
    code: str
    message: str
    request_id: str = ""
    similarity: Decimal | None = None
    real_name: str = field(default="", repr=False)
    number_digest: str = field(default="", repr=False)
    number_masked: str = field(default="", repr=False)


FAILURES = {
    "FailedOperation.CompareFail": "身份证人像比对未通过，请上传本人真实、清晰的身份证人像面。",
    "FailedOperation.CompareLowSimilarity": "身份证人像比对未通过，请上传本人真实、清晰的身份证人像面。",
    "FailedOperation.IdFormatError": "身份证号码识别有误，请重新上传清晰的人像面。",
    "FailedOperation.IdNameMisMatch": "身份证上的姓名与证件号核验不一致，请核对证件或联系客服。",
    "FailedOperation.IdNoExistSystem": "权威库未查到该身份证，请核对证件或联系客服。",
    "FailedOperation.IdPhotoNoExist": "权威库暂无该证件照片，请联系客服核实。",
    "FailedOperation.IdPhotoPoorQuality": "证件照片质量不足，请上传清晰照片后重试。",
    "FailedOperation.ImageBlur": "身份证照片模糊，请重新拍摄。",
    "FailedOperation.ImageDecodeFailed": "身份证照片无法识别，请重新上传。",
    "FailedOperation.ImageNoIdCard": "未识别到身份证，请上传身份证人像面。",
    "FailedOperation.ImageSizeTooLarge": "身份证照片尺寸不符合要求，请重新拍摄。",
    "FailedOperation.OcrFailed": "身份证文字识别失败，请上传清晰、完整的人像面。",
    "FailedOperation.OcrWarningOccurred": "身份证照片触发质量告警，请上传清晰、完整的人像面。",
}
UNAVAILABLE = "身份证验真服务暂不可用，请稍后重试；持续失败请联系客服。"


def credentials():
    secret_id = settings.TENCENT_FACEID_SECRET_ID
    secret_key = settings.TENCENT_FACEID_SECRET_KEY
    if not secret_id and not secret_key:
        secret_id, secret_key = settings.TENCENT_CLOUD_SECRET_ID, settings.TENCENT_CLOUD_SECRET_KEY
    return secret_id, secret_key


def safe_token(value, limit):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,%d}" % limit, value) else ""


def error_result(code, request_id=""):
    safe_code = safe_token(code, 128) or "InvalidResponse"
    return CheckResult(
        "failed" if safe_code in FAILURES else "error", safe_code,
        FAILURES.get(safe_code, UNAVAILABLE), safe_token(request_id, 64),
    )


def read_identity_image(asset):
    """Read only the server-selected COS object; never fetch client-supplied URLs."""
    client = CosS3Client(CosConfig(
        Region=settings.COS_REGION, SecretId=settings.TENCENT_CLOUD_SECRET_ID,
        SecretKey=settings.TENCENT_CLOUD_SECRET_KEY, Scheme="https", Timeout=8,
    ), retry=0)
    response = client.get_object(Bucket=settings.COS_BUCKET, Key=asset.object_key)
    stream = response["Body"].get_raw_stream()
    try:
        payload = stream.read(8 * 1024 * 1024 + 1)
    finally:
        stream.close()
    if not payload or len(payload) > 8 * 1024 * 1024:
        raise ValueError("Invalid identity image size")
    return payload


def encode_image(payload):
    # Legacy WebP and normalized HEIC uploads must become supported JPEG/PNG.
    # Keep the existing burned-in watermark; strip EXIF/GPS from the outbound image.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(payload)) as source:
            if source.width * source.height > 25_000_000:
                raise ValueError("Image dimensions exceed limit")
            picture = ImageOps.exif_transpose(source).convert("RGB")
        picture.thumbnail((3000, 3000))
        for quality in (92, 85, 75):
            output = BytesIO()
            picture.save(output, format="JPEG", quality=quality)
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            if len(encoded) <= 7 * 1024 * 1024:
                return encoded
    raise ValueError("Encoded image exceeds Tencent limit")


def check_identity_photo(*, asset, application_real_name):
    """No automatic retries: this API can bill for each request."""
    import hashlib
    import hmac

    try:
        encoded = encode_image(read_identity_image(asset))
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombWarning,
            Image.DecompressionBombError):
        return CheckResult("failed", "InvalidImage", "身份证照片无法处理，请重新上传清晰的人像面。")
    except Exception:
        # COS errors may contain signed URLs. Never log their text/tracebacks.
        return error_result("ImageReadFailed")
    secret_id, secret_key = credentials()
    if not secret_id or not secret_key:
        return error_result("NotConfigured")
    client = FaceidClient(Credential(secret_id, secret_key), "", ClientProfile(
        httpProfile=HttpProfile(endpoint="faceid.tencentcloudapi.com", reqMethod="POST",
                                reqTimeout=settings.TENCENT_FACEID_TIMEOUT_SECONDS),
        retryer=NoopRetryer(),
    ))
    request = CheckIdCardInformationRequest()
    request.ImageBase64 = encoded
    # PS/occlusion checks conflict with our mandatory platform watermark.
    # Quality is still enforced; this endpoint is NOT live selfie verification.
    request.Config = '{"Quality":true}'
    try:
        response = client.CheckIdCardInformation(request)
    except TencentCloudSDKException as exc:
        return error_result(exc.get_code(), exc.get_request_id())
    except Exception:
        return error_result("TransportError")
    if response.Result != "Success":
        return error_result(response.Result, response.RequestId)
    request_id = safe_token(response.RequestId, 64)
    try:
        similarity = Decimal(str(response.Sim))
        if not similarity.is_finite() or not 0 <= similarity <= 100 or not request_id:
            return error_result("InvalidResponse")
    except (InvalidOperation, ValueError, TypeError):
        return error_result("InvalidResponse")
    if similarity < 70:
        return error_result("FailedOperation.CompareLowSimilarity", request_id)
    if response.Warnings:
        return error_result("FailedOperation.OcrWarningOccurred", request_id)
    if not isinstance(response.Name, str) or not isinstance(response.IdNum, str):
        return error_result("InvalidResponse", request_id)
    name = response.Name.strip()
    if not name or len(name) > 50 or not application_real_name.strip():
        return error_result("InvalidResponse", request_id)
    if name != application_real_name.strip():
        return CheckResult("failed", "NameMismatch", "身份证识别姓名与入驻申请姓名不一致，请联系客服核实并更正申请姓名后重试。", request_id)
    number = response.IdNum.strip().upper()
    if not re.fullmatch(r"[0-9]{17}[0-9X]", number):
        return error_result("InvalidResponse", request_id)
    digest = hmac.new(settings.SECRET_KEY.encode(), number.encode(), hashlib.sha256).hexdigest()
    return CheckResult("passed", "Success", "身份证人像面验真通过，仍需平台审核其余认证材料。",
                       request_id, similarity.quantize(Decimal("0.01")),
                       name, digest, f"{number[:4]}**********{number[-4:]}")
