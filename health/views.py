from django.core.cache import cache
from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.status import HTTP_503_SERVICE_UNAVAILABLE
from rest_framework.views import APIView


class HealthView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(responses={200: dict})
    def get(self, request):
        return Response({"data": {"status": "ok", "service": "dazzy-api"}})


class ReadinessView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(responses={200: dict, 503: dict})
    def get(self, request):
        checks: dict[str, bool] = {"database": False, "cache": False}
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                checks["database"] = cursor.fetchone() == (1,)
        except Exception:
            pass
        try:
            cache.get("healthcheck")
            checks["cache"] = True
        except Exception:
            pass
        ready = all(checks.values())
        return Response(
            {"data": {"status": "ready" if ready else "unavailable", "checks": checks}},
            status=200 if ready else HTTP_503_SERVICE_UNAVAILABLE,
        )
