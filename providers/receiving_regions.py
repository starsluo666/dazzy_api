"""Pinned official Huifu region data; never infer a bank code from a display name."""
import json
from functools import lru_cache
from pathlib import Path

from rest_framework.exceptions import ValidationError


@lru_cache(maxsize=1)
def bank_regions():
    source = json.loads(Path(__file__).with_name("data").joinpath("huifu-regions.json").read_text(encoding="utf-8-sig"))
    return [
        {"name": name, "code": province["val"], "cities": [
            # Some prefectures without districts (e.g. 嘉峪关市) are leaf strings.
            {"name": city_name, "code": city["val"] if isinstance(city, dict) else city}
            for city_name, city in province["items"].items()
        ]}
        for name, province in source.items()
    ]


def validate_bank_region(province_code, city_code):
    for province in bank_regions():
        if province["code"] == province_code:
            for city in province["cities"]:
                if city["code"] == city_code:
                    return province["name"], city["name"]
    raise ValidationError({"bank_city_code": "请重新选择银行所在省市。"})
