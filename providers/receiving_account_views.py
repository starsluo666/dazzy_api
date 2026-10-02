from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .receiving_accounts import (
    clear_receiving_account,
    receiving_account_data,
    save_receiving_account,
)
from .views import current_approved_provider


@method_decorator(sensitive_post_parameters(), name="dispatch")
class CurrentProviderReceivingAccountView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "provider_receiving_account"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        return response

    def get(self, request):
        return Response({"data": receiving_account_data(current_approved_provider(request))})

    def put(self, request):
        provider = current_approved_provider(request)
        save_receiving_account(provider=provider, data=request.data)
        return Response({"data": receiving_account_data(provider)})

    def delete(self, request):
        # Always allow withdrawing locally saved materials, even with collection closed.
        clear_receiving_account(current_approved_provider(request))
        return Response(status=204)
