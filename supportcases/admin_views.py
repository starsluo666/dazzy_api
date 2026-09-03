from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backoffice.access import resolve_admin_access

from .admin_services import (
    add_operator_reply,
    admin_support_case_queryset,
    review_support_case,
)
from .models import SupportCase
from .serializers import (
    AdminSupportCaseListQuerySerializer,
    AdminSupportCaseActionSerializer,
    AdminSupportCaseSerializer,
    SupportCaseReplySerializer,
)


class AdminSupportCaseListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("support.case.view")
        query = AdminSupportCaseListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = admin_support_case_queryset(access)
        if city_code := params.get("city_code"):
            queryset = queryset.filter(city_code=city_code)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(case_no__icontains=keyword)
                | Q(reporter__nickname__icontains=keyword)
                | Q(reporter__phone__icontains=keyword)
                | Q(description__icontains=keyword)
                | Q(provider__user__nickname__icontains=keyword)
                | Q(provider_order__order_no__icontains=keyword)
                | Q(activity__title__icontains=keyword)
            )
        summary_queryset = queryset
        summary = {
            "total": summary_queryset.count(),
            "pending": summary_queryset.filter(status=SupportCase.Status.PENDING).count(),
            "processing": summary_queryset.filter(
                status=SupportCase.Status.PROCESSING
            ).count(),
            "reviewing": summary_queryset.filter(
                status=SupportCase.Status.REVIEWING
            ).count(),
            "resolved": summary_queryset.filter(
                status__in=(
                    SupportCase.Status.RESOLVED,
                    SupportCase.Status.REJECTED,
                    SupportCase.Status.CLOSED,
                )
            ).count(),
        }
        if status := params.get("status"):
            queryset = queryset.filter(status=status)
        elif params.get("status_group") == "terminal":
            queryset = queryset.filter(
                status__in=(
                    SupportCase.Status.RESOLVED,
                    SupportCase.Status.REJECTED,
                    SupportCase.Status.CLOSED,
                )
            )
        if case_type := params.get("case_type"):
            queryset = queryset.filter(case_type=case_type)
        if target_type := params.get("target_type"):
            queryset = queryset.filter(target_type=target_type)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset[(page - 1) * page_size : page * page_size]
        return Response(
            {
                "data": {
                    "items": AdminSupportCaseSerializer(items, many=True).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "summary": summary,
                }
            }
        )


class AdminSupportCaseDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, case_no):
        access = resolve_admin_access(request.user)
        access.require("support.case.view")
        case = get_object_or_404(
            admin_support_case_queryset(access), case_no=case_no
        )
        return Response({"data": AdminSupportCaseSerializer(case).data})


class AdminSupportCaseActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        access = resolve_admin_access(request.user)
        access.require("support.case.manage")
        serializer = AdminSupportCaseActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        case = review_support_case(
            case_no=case_no,
            actor=request.user,
            access=access,
            request=request,
            **serializer.validated_data,
        )
        return Response({"data": AdminSupportCaseSerializer(case).data})


class AdminSupportCaseReplyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        access = resolve_admin_access(request.user)
        access.require("support.case.manage")
        serializer = SupportCaseReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        case = add_operator_reply(
            case_no=case_no,
            content=serializer.validated_data["content"],
            actor=request.user,
            access=access,
            request=request,
        )
        return Response({"data": AdminSupportCaseSerializer(case).data}, status=201)
