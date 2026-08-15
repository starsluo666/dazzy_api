from functools import lru_cache

from django.conf import settings
from qcloud_cos import CosConfig, CosS3Client


@lru_cache(maxsize=1)
def _cos_client() -> CosS3Client:
    config = CosConfig(
        Region=settings.COS_REGION,
        SecretId=settings.TENCENT_CLOUD_SECRET_ID,
        SecretKey=settings.TENCENT_CLOUD_SECRET_KEY,
        Scheme="https",
    )
    return CosS3Client(config)


def build_media_url(object_key: str, *, private: bool = False) -> str | None:
    if not object_key:
        return None
    ttl = settings.COS_SIGNED_PRIVATE_URL_TTL if private else settings.COS_SIGNED_PUBLIC_URL_TTL
    return _cos_client().get_presigned_url(
        Method="GET",
        Bucket=settings.COS_BUCKET,
        Key=object_key,
        Expired=ttl,
    )
