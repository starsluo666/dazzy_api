from django.shortcuts import get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .coupon_campaigns import campaign_payload, claim_campaign
from .models import CouponCampaign


class CouponCampaignDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, campaign_id):
        campaign = get_object_or_404(
            CouponCampaign.objects.select_related("banner", "template").exclude(published_at=None),
            public_id=campaign_id,
        )
        claim = campaign.claims.select_related("coupon__template").filter(user=request.user).first() if request.user.is_authenticated else None
        response = Response({"data": campaign_payload(campaign, claim=claim)})
        response["Cache-Control"] = "private, no-store"
        return response


class CouponCampaignClaimView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, campaign_id):
        return Response({"data": claim_campaign(campaign_id=campaign_id, user=request.user)})
