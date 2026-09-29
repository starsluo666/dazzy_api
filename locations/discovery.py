def default_discovery_cities():
    """Initial open cities; ordering determines the default browsing city."""
    return [
        {"city_code": "130400", "city_name": "邯郸市"},
        {"city_code": "110100", "city_name": "北京市"},
        {"city_code": "310100", "city_name": "上海市"},
    ]


def discovery_cities():
    from backoffice.models import PlatformOperationSetting

    configured = PlatformOperationSetting.objects.filter(singleton_key="default").values_list(
        "discovery_cities", flat=True
    ).first()
    return configured if configured is not None else default_discovery_cities()
