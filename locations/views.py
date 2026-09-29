from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from config.throttles import MapProxyBurstThrottle, MapProxyDailyThrottle
from config.geospatial import wgs84_to_gcj02

from .discovery import discovery_cities

from .models import UserAddress
from .serializers import CoordinatesQuerySerializer, UserAddressSerializer
from .tencent import tencent_map


def lock_user_addresses(user):
    """Serialize address mutations for one user, including creation of their first row."""
    get_user_model().objects.select_for_update().only("pk").get(pk=user.pk)


class DiscoveryCityListView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"data": {"items": discovery_cities()}})


class DiscoveryLocateView(APIView):
    """Public, IP-throttled city lookup. Do not expose addresses or persist device locations."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [MapProxyBurstThrottle, MapProxyDailyThrottle]

    def post(self, request):
        query = CoordinatesQuerySerializer(data=request.data)
        query.is_valid(raise_exception=True)
        lng, lat = wgs84_to_gcj02(**query.validated_data)
        location = tencent_map.reverse_geocode(f"{lng:.7f}", f"{lat:.7f}")
        city = next((city for city in discovery_cities()
                     if city["city_code"] == location["city_code"]), None)
        return Response({"data": {
            "city_code": location["city_code"],
            "city_name": city["city_name"] if city else location["city_name"],
            "is_open": city is not None,
            "longitude": f"{lng:.7f}",
            "latitude": f"{lat:.7f}",
        }}, headers={"Cache-Control": "no-store"})


class PlaceSearchView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [MapProxyBurstThrottle, MapProxyDailyThrottle]

    def get(self, request):
        keyword = request.query_params.get("keyword", "").strip()
        if len(keyword) < 2:
            return Response({"data": {"items": []}})
        region = request.query_params.get("region", settings.TENCENT_MAP_DEFAULT_REGION).strip()
        return Response({"data": {"items": tencent_map.search(keyword, region)}})


class ReverseGeocodeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [MapProxyBurstThrottle, MapProxyDailyThrottle]

    def get(self, request):
        query = CoordinatesQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response({
            "data": tencent_map.reverse_geocode(
                query.validated_data["longitude"],
                query.validated_data["latitude"],
            )
        })


class UserAddressListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        addresses = UserAddress.objects.filter(user=request.user)[:20]
        return Response({"data": {"items": UserAddressSerializer(addresses, many=True).data}})

    @transaction.atomic
    def post(self, request):
        lock_user_addresses(request.user)
        if UserAddress.objects.filter(user=request.user).count() >= 20:
            return Response({"detail": "常用地址最多保存20个。"}, status=400)
        serializer = UserAddressSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": serializer.data}, status=201)


class UserAddressDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, address_id):
        return get_object_or_404(UserAddress, id=address_id, user=request.user)

    def get(self, request, address_id):
        return Response({"data": UserAddressSerializer(self.get_object(request, address_id)).data})

    @transaction.atomic
    def patch(self, request, address_id):
        lock_user_addresses(request.user)
        address = self.get_object(request, address_id)
        serializer = UserAddressSerializer(
            address, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": serializer.data})

    @transaction.atomic
    def delete(self, request, address_id):
        lock_user_addresses(request.user)
        address = self.get_object(request, address_id)
        was_default = address.is_default
        address.delete()
        if was_default:
            replacement = UserAddress.objects.filter(user=request.user).order_by("-updated_at").first()
            if replacement:
                replacement.is_default = True
                replacement.save(update_fields=("is_default", "updated_at"))
        return Response(status=204)
