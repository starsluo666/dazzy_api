from pathlib import PurePosixPath

from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import build_media_url


class HomeCardAssetView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        base = PurePosixPath(settings.COS_PUBLIC_PREFIX) / "demo" / "home-cards"
        return Response(
            {
                "data": {
                    "provider_companion_url": build_media_url(
                        str(base / "provider-companion.webp")
                    ),
                    "group_activity_url": build_media_url(str(base / "group-activity.webp")),
                }
            }
        )
