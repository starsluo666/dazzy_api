from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle


class MapProxyBurstThrottle(UserRateThrottle):
    scope = "map_proxy_burst"


class MapProxyDailyThrottle(UserRateThrottle):
    scope = "map_proxy_daily"


class SmsSendIpDailyThrottle(SimpleRateThrottle):
    scope = "auth_sms_send_ip_daily"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
