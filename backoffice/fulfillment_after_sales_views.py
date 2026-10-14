from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import resolve_admin_access
from .serializers import ProviderOrderAfterSalesCaseSerializer, ProviderOrderAdminSerializer
from orders import fulfillment_after_sales as service


class IntakeInput(serializers.Serializer):
    action = serializers.ChoiceField(choices=("transfer", "investigate"), default="transfer")
    revision = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(min_length=5, max_length=1000, trim_whitespace=True)


class PreviewInput(serializers.Serializer):
    revision = serializers.IntegerField(min_value=1)
    responsibility = serializers.ChoiceField(choices=("customer", "provider", "both", "neither"))
    mode = serializers.ChoiceField(choices=("policy", "manual"))
    occurred_at = serializers.DateTimeField()
    customer_no_show = serializers.BooleanField(default=False)
    component_refunds = serializers.DictField(child=serializers.IntegerField(min_value=0), required=False)
    result_note = serializers.CharField(min_length=5, max_length=1000, trim_whitespace=True)
    facts_confirmed = serializers.BooleanField()

    def validate_facts_confirmed(self, value):
        if value is not True:
            raise serializers.ValidationError("请先核实双方陈述、履约证据和取消或结束时间。")
        return value


class OrderFulfillmentAfterSalesView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, order_no):
        access = resolve_admin_access(request.user)
        data = IntakeInput(data=request.data)
        data.is_valid(raise_exception=True)
        values = dict(data.validated_data)
        action = values.pop("action")
        if action == "investigate":
            order = service.investigate(order_no=order_no, access=access, request=request, **values)
            return Response({"data": ProviderOrderAdminSerializer(order).data})
        case, created = service.transfer(order_no=order_no, access=access, request=request, **values)
        return Response({"data": ProviderOrderAfterSalesCaseSerializer(case).data}, status=201 if created else 200)


class FulfillmentCaseContextView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, case_no):
        return Response({"data": service.context(case_no=case_no, access=resolve_admin_access(request.user), request=request)})


class FulfillmentCasePreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        data = PreviewInput(data=request.data)
        data.is_valid(raise_exception=True)
        inputs = dict(data.validated_data)
        inputs["occurred_at"] = inputs["occurred_at"].isoformat()
        return Response({"data": service.preview(case_no=case_no, inputs=inputs,
            access=resolve_admin_access(request.user), request=request)})


class FulfillmentCaseResolveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        class Input(serializers.Serializer):
            token = serializers.CharField(max_length=15000)
        data = Input(data=request.data)
        data.is_valid(raise_exception=True)
        case = service.resolve(case_no=case_no, token=data.validated_data["token"],
            access=resolve_admin_access(request.user), request=request)
        return Response({"data": ProviderOrderAfterSalesCaseSerializer(case).data})
