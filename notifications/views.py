from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UserNotification
from .serializers import (
    NotificationListQuerySerializer,
    NotificationReadAllSerializer,
    UserNotificationSerializer,
)


def notification_summary(queryset):
    aggregate = queryset.aggregate(
        total=Count("id"),
        unread=Count("id", filter=Q(read_at__isnull=True)),
        **{
            f"{category}_unread": Count(
                "id",
                filter=Q(category=category, read_at__isnull=True),
            )
            for category in UserNotification.Category.values
        },
    )
    category_unread = {
        category: aggregate.pop(f"{category}_unread")
        for category in UserNotification.Category.values
    }
    return {**aggregate, "category_unread": category_unread}


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = NotificationListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        base_queryset = UserNotification.objects.filter(recipient=request.user)
        summary = notification_summary(base_queryset)
        queryset = base_queryset
        if category := params.get("category"):
            queryset = queryset.filter(category=category)
        if "is_read" in params:
            queryset = queryset.filter(read_at__isnull=not params["is_read"])
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset[(page - 1) * page_size : page * page_size]
        return Response(
            {
                "data": {
                    "items": UserNotificationSerializer(items, many=True).data,
                    "pagination": {
                        "page": page,
                        "page_size": page_size,
                        "total": total,
                    },
                    "summary": summary,
                }
            }
        )


class NotificationSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = UserNotification.objects.filter(recipient=request.user)
        return Response({"data": notification_summary(queryset)})


class NotificationReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, public_id):
        notification = get_object_or_404(
            UserNotification, public_id=public_id, recipient=request.user
        )
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=("read_at",))
        return Response({"data": UserNotificationSerializer(notification).data})


class NotificationReadAllView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = NotificationReadAllSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        queryset = UserNotification.objects.filter(
            recipient=request.user, read_at__isnull=True
        )
        if category := serializer.validated_data.get("category"):
            queryset = queryset.filter(category=category)
        updated = queryset.update(read_at=timezone.now())
        remaining = UserNotification.objects.filter(
            recipient=request.user, read_at__isnull=True
        ).count()
        return Response({"data": {"updated": updated, "unread": remaining}})
