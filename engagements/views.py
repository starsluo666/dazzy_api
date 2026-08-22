from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from activities.models import Activity
from activities.selectors import with_participant_count
from activities.serializers import ActivityListItemSerializer
from providers.selectors import public_providers
from providers.serializers import ProviderListItemSerializer

from .models import BrowsingHistory, ProviderFavorite


def record_history(*, user, target_type, provider=None, activity=None):
    lookup = {"user": user, "target_type": target_type}
    lookup["provider" if provider else "activity"] = provider or activity
    history, created = BrowsingHistory.objects.get_or_create(**lookup)
    if not created:
        BrowsingHistory.objects.filter(pk=history.pk).update(view_count=F("view_count") + 1, viewed_at=timezone.now())
        history.refresh_from_db()
    stale_ids = list(BrowsingHistory.objects.filter(user=user).values_list("id", flat=True)[500:])
    if stale_ids:
        BrowsingHistory.objects.filter(id__in=stale_ids).delete()
    return history


class ProviderFavoriteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, public_id):
        provider = get_object_or_404(public_providers(), user__public_id=public_id)
        favorite, _ = ProviderFavorite.objects.get_or_create(user=request.user, provider=provider)
        return Response({"data": {"is_favorited": True, "created_at": favorite.created_at}}, status=201)

    def delete(self, request, public_id):
        ProviderFavorite.objects.filter(user=request.user, provider__user__public_id=public_id).delete()
        return Response(status=204)


class ProviderFavoriteListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(50, max(1, int(request.query_params.get("page_size", 20))))
        favorites = ProviderFavorite.objects.filter(user=request.user, provider__status="approved").select_related("provider__user").prefetch_related("provider__services__category")
        total = favorites.count()
        items = [favorite.provider for favorite in favorites[(page - 1) * page_size:page * page_size]]
        return Response({"data": {"items": ProviderListItemSerializer(items, many=True, context={"request": request}).data, "pagination": {"page": page, "page_size": page_size, "total": total}}})


class ProviderHistoryRecordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, public_id):
        provider = get_object_or_404(public_providers(), user__public_id=public_id)
        history = record_history(user=request.user, target_type=BrowsingHistory.TargetType.PROVIDER, provider=provider)
        return Response({"data": {"id": history.pk, "view_count": history.view_count}}, status=201)


class ActivityHistoryRecordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        activity = get_object_or_404(Activity, pk=pk)
        history = record_history(user=request.user, target_type=BrowsingHistory.TargetType.ACTIVITY, activity=activity)
        return Response({"data": {"id": history.pk, "view_count": history.view_count}}, status=201)


class BrowsingHistoryListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        target_type = request.query_params.get("type", "all")
        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(50, max(1, int(request.query_params.get("page_size", 20))))
        queryset = BrowsingHistory.objects.filter(user=request.user).select_related("provider__user", "activity__category", "activity__organizer", "activity__cover")
        if target_type in ("provider", "activity"):
            queryset = queryset.filter(target_type=target_type)
        total = queryset.count()
        histories = list(queryset[(page - 1) * page_size:page * page_size])
        activity_ids = [item.activity_id for item in histories if item.activity_id]
        activities = {item.pk: item for item in with_participant_count(Activity.objects.filter(pk__in=activity_ids).select_related("category", "organizer", "cover"))}
        items = []
        for history in histories:
            target = history.provider if history.provider_id else activities.get(history.activity_id)
            if not target:
                continue
            serializer = ProviderListItemSerializer(target, context={"request": request}) if history.provider_id else ActivityListItemSerializer(target)
            items.append({"id": history.pk, "target_type": history.target_type, "viewed_at": history.viewed_at, "view_count": history.view_count, "target": serializer.data})
        return Response({"data": {"items": items, "pagination": {"page": page, "page_size": page_size, "total": total}}})

    def delete(self, request):
        BrowsingHistory.objects.filter(user=request.user).delete()
        return Response(status=204)


class BrowsingHistoryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        get_object_or_404(BrowsingHistory, pk=pk, user=request.user).delete()
        return Response(status=204)
