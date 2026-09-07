import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from django.conf import settings
from rest_framework.exceptions import APIException, ValidationError


class TencentMapUnavailable(APIException):
    status_code = 503
    default_detail = "地图服务暂时不可用，请稍后重试。"
    default_code = "map_service_unavailable"


class TencentMapQuotaExceeded(APIException):
    status_code = 429
    default_detail = "地图接口今日额度已用完，请稍后再试。"
    default_code = "map_quota_exceeded"


@dataclass(frozen=True)
class RouteResult:
    distance_km: Decimal
    duration_minutes: int


class TencentMapClient:
    base_url = "https://apis.map.qq.com"

    @staticmethod
    def city_code_from_adcode(adcode: object) -> str:
        """Normalize a district/city adcode to a prefecture-level city code."""
        code = str(adcode or "").strip()
        if len(code) != 6 or not code.isdigit():
            return ""
        if code[:2] in {"11", "12", "31", "50"}:
            return f"{code[:2]}0100"
        return f"{code[:4]}00"

    def _get(self, path: str, params: dict) -> dict:
        signed_params = {**params, "key": settings.TENCENT_MAP_WEB_SERVICE_KEY}
        # 腾讯要求参数按名称排序，并对“未 URL 编码”的 path + query + SK 计算 MD5。
        ordered_params = sorted(signed_params.items())
        raw_query = "&".join(f"{key}={value}" for key, value in ordered_params)
        signature = hashlib.md5(
            f"{path}?{raw_query}{settings.TENCENT_MAP_WEB_SERVICE_SK}".encode()
        ).hexdigest()
        url = f"{self.base_url}{path}?{urlencode([*ordered_params, ('sig', signature)])}"
        try:
            with urlopen(url, timeout=settings.TENCENT_MAP_TIMEOUT_SECONDS) as response:  # noqa: S310
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise TencentMapUnavailable() from exc
        if payload.get("status") == 121:
            raise TencentMapQuotaExceeded()
        if payload.get("status") != 0:
            raise ValidationError({"map": payload.get("message") or "地图请求失败。"})
        return payload

    def search(self, keyword: str, region: str, page_size: int = 20) -> list[dict]:
        payload = self._get(
            "/ws/place/v1/suggestion/",
            {"keyword": keyword, "region": region, "region_fix": 1, "page_size": page_size},
        )
        return [
            {
                "id": item.get("id", ""),
                "name": item.get("title", ""),
                "address": item.get("address", ""),
                "city_name": item.get("city", ""),
                "city_code": self.city_code_from_adcode(item.get("adcode")),
                "district_name": item.get("district", ""),
                "longitude": item["location"]["lng"],
                "latitude": item["location"]["lat"],
            }
            for item in payload.get("data", [])
            if item.get("location")
        ]

    def reverse_geocode(self, longitude: Decimal, latitude: Decimal) -> dict:
        payload = self._get(
            "/ws/geocoder/v1/",
            {"location": f"{latitude},{longitude}", "get_poi": 0},
        )["result"]
        component = payload.get("address_component", {})
        ad_info = payload.get("ad_info", {})
        return {
            "name": payload.get("formatted_addresses", {}).get("recommend")
            or payload.get("title")
            or payload.get("address"),
            "address": payload.get("address", ""),
            "city_name": component.get("city", ""),
            "city_code": self.city_code_from_adcode(ad_info.get("adcode")),
            "district_name": component.get("district", ""),
            "longitude": payload["location"]["lng"],
            "latitude": payload["location"]["lat"],
        }

    def driving_route(
        self, from_longitude: Decimal, from_latitude: Decimal,
        to_longitude: Decimal, to_latitude: Decimal,
    ) -> RouteResult:
        payload = self._get(
            "/ws/direction/v1/driving/",
            {
                "from": f"{from_latitude},{from_longitude}",
                "to": f"{to_latitude},{to_longitude}",
                "policy": "LEAST_TIME",
            },
        )
        routes = payload.get("result", {}).get("routes", [])
        if not routes:
            raise ValidationError({"meeting_address": "无法规划到该地点的驾车路线。"})
        route = routes[0]
        return RouteResult(
            distance_km=(Decimal(str(route["distance"])) / Decimal(1000)).quantize(Decimal("0.01")),
            # Direction API 的 duration 单位为分钟。
            duration_minutes=max(1, int(route["duration"])),
        )


tencent_map = TencentMapClient()
