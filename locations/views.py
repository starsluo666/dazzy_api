from decimal import Decimal

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UserAddress
from .serializers import UserAddressSerializer
from .tencent import tencent_map


class PlaceSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        keyword = request.query_params.get("keyword", "").strip()
        if len(keyword) < 2:
            return Response({"data": {"items": []}})
        region = request.query_params.get("region", settings.TENCENT_MAP_DEFAULT_REGION).strip()
        return Response({"data": {"items": tencent_map.search(keyword, region)}})


class ReverseGeocodeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        longitude = Decimal(request.query_params["longitude"])
        latitude = Decimal(request.query_params["latitude"])
        return Response({"data": tencent_map.reverse_geocode(longitude, latitude)})


class UserAddressListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        addresses = UserAddress.objects.filter(user=request.user)[:20]
        return Response({"data": {"items": UserAddressSerializer(addresses, many=True).data}})

    def post(self, request):
        serializer = UserAddressSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": serializer.data}, status=201)


class UserAddressDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, address_id):
        get_object_or_404(UserAddress, id=address_id, user=request.user).delete()
        return Response(status=204)
