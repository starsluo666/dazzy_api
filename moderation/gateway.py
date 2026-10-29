"""Tencent Cloud TMS/IMS/VM official SDK adapters; no callbacks or raw response logs."""

import base64
import json
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.views.decorators.debug import sensitive_variables


class DetectionUnavailable(Exception):
    def __init__(self, code="detection_unavailable"):
        self.code = code
        super().__init__("内容检测暂未完成，已转人工复核。")


@sensitive_variables()
def _call(product, action, payload):
    from tencentcloud.common.credential import Credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile

    if not settings.TENCENT_CONTENT_REVIEW_ENABLED:
        raise DetectionUnavailable("machine_disabled")
    secret_id = settings.TENCENT_CONTENT_REVIEW_SECRET_ID
    secret_key = settings.TENCENT_CONTENT_REVIEW_SECRET_KEY
    if not secret_id or not secret_key or not settings.TENCENT_CONTENT_REVIEW_REGION:
        raise DetectionUnavailable("configuration_missing")
    if product == "tms":
        from tencentcloud.tms.v20201229.tms_client import TmsClient as Client
        from tencentcloud.tms.v20201229 import models
    elif product == "ims":
        from tencentcloud.ims.v20201229.ims_client import ImsClient as Client
        from tencentcloud.ims.v20201229 import models
    else:
        from tencentcloud.vm.v20210922.vm_client import VmClient as Client
        from tencentcloud.vm.v20210922 import models
    try:
        profile = ClientProfile(
            httpProfile=HttpProfile(
                endpoint=f"{product}.tencentcloudapi.com",
                reqTimeout=15,
            )
        )
        client = Client(
            Credential(secret_id, secret_key), settings.TENCENT_CONTENT_REVIEW_REGION, profile
        )
        request = getattr(models, f"{action}Request")()
        request.from_json_string(json.dumps(payload, ensure_ascii=False))
        response = getattr(client, action)(request)
        data = json.loads(response.to_json_string())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:
        raise DetectionUnavailable() from None


def _identifier(value, maximum=128):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_@#-]{1,%d}" % maximum, value))


def _decision(data):
    if data.get("Suggestion") not in ("Pass", "Review", "Block") or not _identifier(
        data.get("RequestId")
    ):
        raise DetectionUnavailable("invalid_response")
    label = data.get("Label")
    score = data.get("Score")
    return {
        "suggestion": data["Suggestion"],
        "request_id": data["RequestId"],
        "label": label if _identifier(label, 64) else "",
        "score": score if type(score) is int and 0 <= score <= 100 else None,
    }


def detect_text(text, data_id):
    if not text or len(text) > 10000:
        raise DetectionUnavailable("text_limit")
    payload = {"Content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "DataId": data_id}
    if policy := settings.TENCENT_CONTENT_REVIEW_TEXT_BIZ_TYPE:
        payload["BizType"] = policy
    data = _call("tms", "TextModeration", payload)
    if data.get("DataId") != data_id:
        raise DetectionUnavailable("identity_mismatch")
    return _decision(data)


def media_url(asset):
    from mediafiles.services import build_media_url

    if asset.scope != "public" or asset.status not in ("uploaded", "verified"):
        raise DetectionUnavailable("asset_unavailable")
    url = build_media_url(asset.object_key)
    parsed = urlsplit(url or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DetectionUnavailable("media_url_invalid")
    return url


def detect_image(asset, data_id):
    payload = {"FileUrl": media_url(asset), "DataId": data_id}
    if policy := settings.TENCENT_CONTENT_REVIEW_IMAGE_BIZ_TYPE:
        payload["BizType"] = policy
    data = _call("ims", "ImageModeration", payload)
    if data.get("DataId") != data_id:
        raise DetectionUnavailable("identity_mismatch")
    return _decision(data)


def create_video(asset, data_id):
    policy = settings.TENCENT_CONTENT_REVIEW_VIDEO_BIZ_TYPE
    if not isinstance(policy, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,32}", policy):
        raise DetectionUnavailable("video_policy_missing")
    data = _call(
        "vm",
        "CreateVideoModerationTask",
        {
            "BizType": policy,
            "Type": "VIDEO",
            "Tasks": [
                {"DataId": data_id, "Input": {"Type": "URL", "Url": media_url(asset)}},
            ],
        },
    )
    results = data.get("Results")
    if (
        not isinstance(results, list)
        or len(results) != 1
        or not isinstance(results[0], dict)
        or results[0].get("DataId") != data_id
        or results[0].get("Code") not in ("OK", "200")
        or not _identifier(results[0].get("TaskId"))
        or not _identifier(data.get("RequestId"))
    ):
        raise DetectionUnavailable("video_submission_unknown")
    return {"task_id": results[0]["TaskId"], "request_id": data["RequestId"]}


def query_video(task_id, data_id):
    data = _call("vm", "DescribeTaskDetail", {"TaskId": task_id, "ShowAllSegments": False})
    if data.get("TaskId") != task_id or data.get("DataId") != data_id:
        raise DetectionUnavailable("identity_mismatch")
    if data.get("Status") in ("PENDING", "RUNNING"):
        return None
    if data.get("Status") != "FINISH":
        raise DetectionUnavailable("video_not_finished")
    result = _decision(data)
    # VM returns a list of labels rather than the top-level IMS/TMS Label/Score.
    severity = {"Pass": 0, "Review": 1, "Block": 2}
    labels = data.get("Labels") or []
    if not isinstance(labels, list) or any(
        not isinstance(label, dict) or label.get("Suggestion") not in severity for label in labels
    ):
        raise DetectionUnavailable("invalid_response")
    if labels:
        label = max(labels, key=lambda row: severity[row["Suggestion"]])
        finding = _decision({**label, "RequestId": data["RequestId"]})
        result["label"], result["score"] = finding["label"], finding["score"]
        if severity[finding["suggestion"]] > severity[result["suggestion"]]:
            result["suggestion"] = finding["suggestion"]
    return result
