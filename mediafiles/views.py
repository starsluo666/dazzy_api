from rest_framework.response import Response
from rest_framework.views import APIView

from .services import build_home_card_assets


class HomeCardAssetView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return Response({"data": build_home_card_assets()})
