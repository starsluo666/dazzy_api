from uuid import UUID
from django.db.models import Count, Q

from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User

from .models import Invitation
from .services import campaign_payload, get_campaign_config, viewer_campaign_status


def masked_name(user):
    name = (user.nickname or "新用户").strip()
    return f"{name[:1]}**" if name else "新用户"


def reward_status(invitation):
    return "first_order_rewarded" if invitation.first_order_reward_coupon_id else "registered"


class GrowthCampaignView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        payload = campaign_payload(get_campaign_config())
        payload["viewer"] = viewer_campaign_status(request.user)
        invite_code = request.query_params.get("invite_code", "").strip()
        inviter = None
        if invite_code:
            try:
                parsed_invite_code = UUID(invite_code)
            except ValueError:
                parsed_invite_code = None
            if parsed_invite_code:
                inviter = User.objects.filter(
                    public_id=parsed_invite_code,
                    account_status=User.AccountStatus.ACTIVE,
                    is_active=True,
                ).first()
        payload["invitation_valid"] = bool(inviter)
        payload["inviter_name"] = masked_name(inviter) if inviter else ""
        return Response({"data": payload})


class MyInvitationSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        all_invitations = Invitation.objects.filter(inviter=request.user)
        invitations = list(
            all_invitations
            .select_related(
                "invitee",
                "registration_reward_coupon",
                "first_order_reward_coupon",
            )[:100]
        )
        campaign = campaign_payload(get_campaign_config())
        return Response(
            {
                "data": {
                    "invite_code": str(request.user.public_id),
                    "campaign": campaign,
                    "summary": all_invitations.aggregate(
                        registered_count=Count("pk"),
                        registration_reward_count=Count("pk", filter=Q(registration_reward_coupon__isnull=False)),
                        first_order_reward_count=Count("pk", filter=Q(first_order_reward_coupon__isnull=False)),
                    ),
                    "items": [
                        {
                            "public_id": str(item.public_id),
                            "invitee_name": masked_name(item.invitee),
                            "registered_at": item.registered_at,
                            "status": reward_status(item),
                            "registration_rewarded": bool(item.registration_reward_coupon_id),
                            "first_order_rewarded": bool(item.first_order_reward_coupon_id),
                            "first_order_completed_at": item.first_order_completed_at,
                        }
                        for item in invitations
                    ],
                }
            }
        )
