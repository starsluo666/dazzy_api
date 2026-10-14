def default_discovery_cities():
    """Launch with Handan only; additional cities must be configured explicitly."""
    return [
        {"city_code": "130400", "city_name": "邯郸市"},
    ]


def discovery_cities():
    from backoffice.models import PlatformOperationSetting

    configured = PlatformOperationSetting.objects.filter(singleton_key="default").values_list(
        "discovery_cities", flat=True
    ).first()
    return configured if configured is not None else default_discovery_cities()


def open_service_cities():
    """One source of truth for new service business, not postal/banking addresses."""
    return {city["city_code"]: city["city_name"] for city in discovery_cities()}


def require_open_city(code, *, field="service_city_code"):
    from rest_framework.exceptions import ValidationError

    code = str(code or "").strip()
    if not code:
        raise ValidationError({field: "请选择已开放的服务城市。"})
    name = open_service_cities().get(code)
    if not name:
        raise ValidationError({field: "该城市暂未开放，请重新选择已开放城市。"})
    return code, name
