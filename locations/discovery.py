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
