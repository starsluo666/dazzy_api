from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import resolve_admin_access
from .models import AdminWorkReadReceipt
from .operations_queue import get_queue, queue_queryset, target, work_summary
from .operations_queue import filter_work_queue
from .finance_work import FINANCE_QUEUES, finance_row


class WorkQuery(serializers.Serializer):
    queue = serializers.CharField(max_length=64)
    page = serializers.IntegerField(min_value=1, max_value=100000, default=1)
    unread_only = serializers.BooleanField(default=False)


class ReadInput(serializers.Serializer):
    queue = serializers.CharField(max_length=64)
    object_id = serializers.CharField(max_length=40)
    event_version = serializers.CharField(max_length=160)


class AdminWorkSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        return Response({"data": work_summary(access, request.user)})


class AdminWorkItemsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        query = WorkQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queue = get_queue(access, params["queue"])
        qs = queue_queryset(queue, request.user, timezone.now())
        if params["unread_only"]:
            qs = qs.filter(_read=False)
        total = qs.count()
        offset = (params["page"] - 1) * 20
        rows = qs.order_by("_read", "_work_at", "pk").values(
            "_work_id", "_event", "_work_at", "_read", "_overdue", queue.reference)[offset:offset + 20]
        items = [{"queue": queue.key, "object_id": row["_work_id"], "event_version": row["_event"],
                  "title": queue.label, "reference": row[queue.reference], "created_at": row["_work_at"],
                  "read": row["_read"], "overdue": row["_overdue"], "target": target(queue, row[queue.reference], row["_work_id"])} for row in rows]
        return Response({"data": {"items": items, "total": total, "page": params["page"], "page_size": 20}})

    def post(self, request):
        access = resolve_admin_access(request.user)
        data = ReadInput(data=request.data)
        data.is_valid(raise_exception=True)
        params = data.validated_data
        queue = get_queue(access, params["queue"])
        # Validate the exact, still-visible event. A stale click cannot read a newer event.
        get_object_or_404(queue_queryset(queue, request.user, timezone.now()),
                         _work_id=params["object_id"], _event=params["event_version"])
        AdminWorkReadReceipt.objects.get_or_create(user=request.user, queue_key=queue.key,
            object_id=params["object_id"], event_version=params["event_version"])
        return Response({"data": {"read": True}})


class FinanceWorkQuery(WorkQuery):
    queue = serializers.ChoiceField(choices=FINANCE_QUEUES)
    search = serializers.CharField(required=False, allow_blank=True, max_length=100)


class AdminFinanceWorkView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("order.finance.view")
        access.require("order.finance.manage")
        query = FinanceWorkQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queue = get_queue(access, params["queue"])
        qs = filter_work_queue(queue.queryset, access, {"todo": queue.key, "work_id": request.query_params.get("work_id", "")}, FINANCE_QUEUES)
        if search := params.get("search"):
            qs = qs.filter(**{f"{queue.reference}__icontains": search})
        related = ("provider",) if queue.key == "income_reconciliation" else (
            ("wallet__provider",) if queue.key == "withdrawal_attention" else ("settlement__provider", "settlement__order")
        )
        qs = qs.select_related(*related).order_by(queue.sort_field, "pk")
        total = qs.count()
        offset = (params["page"] - 1) * 20
        return Response({"data": {"items": [finance_row(queue.key, obj) for obj in qs[offset:offset + 20]],
            "total": total, "page": params["page"], "page_size": 20}})


class AdminNotificationChannelsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        resolve_admin_access(request.user)
        from notifications.external_channels import channel_capabilities
        return Response({"data": {"channels": channel_capabilities()}})
