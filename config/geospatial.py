import math
from decimal import Decimal

from django.contrib.gis.geos import Point

_A = 6378245.0
_EE = 0.006693421622965943


def _outside_china(longitude: float, latitude: float) -> bool:
    return not (72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271)


def _transform_latitude(longitude: float, latitude: float) -> float:
    value = -100.0 + 2.0 * longitude + 3.0 * latitude
    value += 0.2 * latitude * latitude + 0.1 * longitude * latitude
    value += 0.2 * math.sqrt(abs(longitude))
    value += (20.0 * math.sin(6.0 * longitude * math.pi) + 20.0 * math.sin(2.0 * longitude * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(latitude * math.pi) + 40.0 * math.sin(latitude / 3.0 * math.pi)) * 2.0 / 3.0
    return value + (160.0 * math.sin(latitude / 12.0 * math.pi) + 320 * math.sin(latitude * math.pi / 30.0)) * 2.0 / 3.0


def _transform_longitude(longitude: float, latitude: float) -> float:
    value = 300.0 + longitude + 2.0 * latitude
    value += 0.1 * longitude * longitude + 0.1 * longitude * latitude
    value += 0.1 * math.sqrt(abs(longitude))
    value += (20.0 * math.sin(6.0 * longitude * math.pi) + 20.0 * math.sin(2.0 * longitude * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(longitude * math.pi) + 40.0 * math.sin(longitude / 3.0 * math.pi)) * 2.0 / 3.0
    return value + (150.0 * math.sin(longitude / 12.0 * math.pi) + 300.0 * math.sin(longitude / 30.0 * math.pi)) * 2.0 / 3.0


def gcj02_to_wgs84(longitude: Decimal | float, latitude: Decimal | float) -> Point:
    """Convert an AMap GCJ-02 coordinate into a WGS84 GeoDjango point."""
    gcj_lng = float(longitude)
    gcj_lat = float(latitude)
    if _outside_china(gcj_lng, gcj_lat):
        return Point(gcj_lng, gcj_lat, srid=4326)

    delta_lat = _transform_latitude(gcj_lng - 105.0, gcj_lat - 35.0)
    delta_lng = _transform_longitude(gcj_lng - 105.0, gcj_lat - 35.0)
    rad_lat = gcj_lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    delta_lat = delta_lat * 180.0 / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    delta_lng = delta_lng * 180.0 / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return Point(gcj_lng - delta_lng, gcj_lat - delta_lat, srid=4326)
