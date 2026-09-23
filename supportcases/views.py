from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backoffice.operation_settings import platform_operation_rules
from orders.models import ProviderOrder

from .serializers import (
    SupportCaseCreateSerializer,
    SupportCaseListQuerySerializer,
    SupportCaseReplySerializer,
    SupportCaseReviewRequestSerializer,
    SupportCaseSerializer,
)
from .services import (
    add_user_reply,
    create_support_case,
    request_case_review,
    support_case_queryset,
)


class SupportCaseListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = SupportCaseListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = support_case_queryset().filter(reporter=request.user)
        if status := params.get("status"):
            queryset = queryset.filter(status=status)
        if case_type := params.get("case_type"):
            queryset = queryset.filter(case_type=case_type)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset[(page - 1) * page_size : page * page_size]
        return Response(
            {
                "data": {
                    "items": SupportCaseSerializer(items, many=True).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                }
            }
        )

    def post(self, request):
        serializer = SupportCaseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        case, created = create_support_case(
            reporter=request.user, validated_data=serializer.validated_data
        )
        return Response(
            {"data": SupportCaseSerializer(case).data, "created": created},
            status=201 if created else 200,
        )


class RewardReportRulesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rules = platform_operation_rules()
        orders = ProviderOrder.objects.filter(
            customer=request.user, status=ProviderOrder.Status.PENDING_REVIEW,
            review_expires_at__gt=timezone.now(),
        ).exclude(support_cases__reward_eligible=True).order_by("-review_expires_at")[:100]
        return Response({"data": {
            "review_timeout_days": rules["provider_order_review_timeout_days"],
            "coupon_amount": rules["report_coupon_amount"],
            "coupon_min_order_amount": rules["report_coupon_min_order_amount"],
            "coupon_valid_days": rules["report_coupon_valid_days"],
            "eligible_orders": [
                {"order_no": order.order_no, "service_name": order.service_name_snapshot,
                 "provider_name": order.provider_name_snapshot,
                 "review_expires_at": order.review_expires_at}
                for order in orders
            ],
        }})


class SupportCaseDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, case_no):
        case = get_object_or_404(
            support_case_queryset(), case_no=case_no, reporter=request.user
        )
        return Response({"data": SupportCaseSerializer(case).data})


class SupportCaseReplyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        serializer = SupportCaseReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        case = get_object_or_404(
            support_case_queryset(), case_no=case_no, reporter=request.user
        )
        case = add_user_reply(
            case=case, reporter=request.user, content=serializer.validated_data["content"]
        )
        return Response({"data": SupportCaseSerializer(case).data}, status=201)


class SupportCaseReviewRequestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        serializer = SupportCaseReviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        case = get_object_or_404(
            support_case_queryset(), case_no=case_no, reporter=request.user
        )
        case = request_case_review(
            case=case, reporter=request.user, reason=serializer.validated_data["reason"]
        )
        return Response({"data": SupportCaseSerializer(case).data})
