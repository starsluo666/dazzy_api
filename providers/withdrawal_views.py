from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response

from .models import ProviderWithdrawal
from .receiving_account_views import CurrentProviderReceivingAccountView
from .views import current_receiving_profile
from .withdrawals import create_withdrawal, query_withdrawal, withdrawal_data


class WithdrawalInput(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1, max_value=999999999999)
    request_key = serializers.UUIDField()


class ProviderWithdrawalView(CurrentProviderReceivingAccountView):
    http_method_names = ["post", "options"]

    def post(self, request):
        data = WithdrawalInput(data=request.data)
        data.is_valid(raise_exception=True)
        record, created = create_withdrawal(
            current_receiving_profile(request), **data.validated_data
        )
        return Response({"data": withdrawal_data(record)}, status=201 if created else 200)


class ProviderWithdrawalRefreshView(CurrentProviderReceivingAccountView):
    http_method_names = ["post", "options"]

    def post(self, request, withdrawal_no):
        record = get_object_or_404(
            ProviderWithdrawal,
            req_seq_id=withdrawal_no,
            wallet__provider=current_receiving_profile(request),
        )
        return Response({"data": withdrawal_data(query_withdrawal(record))})
