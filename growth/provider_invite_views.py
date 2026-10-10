import logging
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.serializers import RegisterSerializer
from accounts.models import User
from accounts.views import auth_payload
from accounts.account_closure import lock_active_user_for_business
from mediafiles.services import delete_public_object
from mediafiles.views import ProviderLifestylePhotoUploadView
from providers.models import ProviderProfile
from providers.serializers import ProviderApplicationSerializer
from providers.services import save_provider_application, submit_provider_application
from .models import ProviderInviteConfig, ProviderInviteReward, ProviderInviteSource
from .provider_invites import (
    service_cities,
    require_available,
    reward_payload,
    source_payload,
    qr_data,
    invite_h5_base,
)


logger = logging.getLogger(__name__)


class InvitedRegistrationInput(RegisterSerializer):
    # Override the unrelated coupon-invitation UUID; clients may not set it here.
    invite_code = serializers.CharField(read_only=True)
    source_code = serializers.RegexField(r"^[A-F0-9]{12}$")
    application_real_name = serializers.CharField(min_length=2, max_length=50)
    application_birth_date = serializers.DateField()
    gender = serializers.ChoiceField(choices=("male", "female"))
    service_city_code = serializers.CharField(max_length=20)
    agreement_accepted = serializers.BooleanField()

    def validate_application_birth_date(self, value):
        return ProviderApplicationSerializer().validate_application_birth_date(value)

    def validate_service_city_code(self, value):
        if value not in service_cities():
            raise serializers.ValidationError("请选择平台已开放的服务城市。")
        return value

    def validate_agreement_accepted(self, value):
        if not value:
            raise serializers.ValidationError("请阅读并同意相关协议及入驻须知。")
        return value


class PublicProviderInviteView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "provider_invite_public"

    def get(self, request, code):
        source = get_object_or_404(
            ProviderInviteSource.objects.select_related("provider_user"), code=code
        )
        config = ProviderInviteConfig.objects.filter(pk=1).first()
        if not config:
            raise ValidationError("达人邀请暂未开放。")
        require_available(source, config)
        # Public response contains no financial amounts, owner IDs or phone numbers.
        return Response(
            {
                "data": {
                    "code": source.code,
                    "name": source.name,
                    "kind": source.kind,
                    "cities": [{"code": c, "name": n} for c, n in service_cities().items()],
                }
            }
        )


class InvitedProviderRegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_register"

    def post(self, request):
        serializer = InvitedRegistrationInput(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        source = get_object_or_404(ProviderInviteSource, code=data["source_code"])
        config = ProviderInviteConfig.objects.filter(pk=1).first()
        if not config:
            raise ValidationError("达人邀请暂未开放。")
        require_available(source, config)
        uploaded = request.FILES.get("file")
        if not uploaded or uploaded.size > 8 * 1024 * 1024:
            raise ValidationError({"file": "请选择一张不超过8MB的近期生活照。"})
        asset = None
        try:
            with transaction.atomic():
                # SMS verified before any persistent user or storage side effect.
                # If upload fails the new account, profile and reward all roll back.
                user = serializer.save()
                user.gender = data["gender"]
                user.save(update_fields=("gender",))
                upload_request = SimpleNamespace(user=user, FILES=request.FILES, data=request.data)
                asset = ProviderLifestylePhotoUploadView().create_asset(upload_request)
                # Coordinate rewards with inviter account closure: user -> config -> source.
                if source.provider_user_id:
                    User.objects.select_for_update().get(pk=source.provider_user_id)
                # Lock only after the potentially slow upload, then recheck availability.
                config = ProviderInviteConfig.objects.select_for_update().get(pk=1)
                source = ProviderInviteSource.objects.select_for_update().get(pk=source.pk)
                require_available(source, config)
                city_name = service_cities().get(data["service_city_code"])
                if not city_name:
                    raise ValidationError("该城市已暂停开放，请重新选择服务城市。")
                profile = save_provider_application(
                    user=user,
                    data={
                        "application_real_name": data["application_real_name"],
                        "application_birth_date": data["application_birth_date"],
                        "service_city_code": data["service_city_code"],
                        "service_city_name": city_name,
                        "lifestyle_photo": asset,
                        "invitation_code": source.code,
                    },
                )
                submit_provider_application(user=user)
                amount = (
                    config.store_reward_amount
                    if source.kind == "store"
                    else config.provider_reward_amount
                )
                if amount <= 0:
                    raise ValidationError("邀请奖励尚未配置，请联系平台。")
                ProviderInviteReward.objects.create(
                    source=source,
                    invitee=user,
                    profile=profile,
                    amount=amount,
                    code_snapshot=source.code,
                    source_name_snapshot=source.name,
                    config_revision=config.revision,
                )
                result = auth_payload(user)
            return Response({"data": result}, status=201)
        except Exception:
            if asset:
                try:
                    delete_public_object(object_key=asset.object_key)
                except Exception:
                    logger.exception("Failed to clean up rolled-back provider invitation photo")
            raise


class MyProviderInvitesView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        user = lock_active_user_for_business(request.user)
        profile = get_object_or_404(ProviderProfile, user=user, status="approved")
        source, _ = ProviderInviteSource.objects.get_or_create(
            provider_user=user,
            defaults={"kind": "provider", "name": profile.display_name or user.nickname},
        )
        return Response({"data": source_payload(source)})

    def get(self, request):
        profile = get_object_or_404(ProviderProfile, user=request.user)
        source = ProviderInviteSource.objects.filter(provider_user=request.user).first()
        config = ProviderInviteConfig.objects.filter(pk=1).first()
        query = serializers.IntegerField(min_value=1).run_validation(
            request.query_params.get("page", 1)
        )
        rewards = ProviderInviteReward.objects.filter(source=source).select_related("invitee")
        summary = list(
            rewards.order_by().values("status").annotate(count=Count("id"), amount=Sum("amount"))
        )
        active = bool(
            config and config.enabled and source and source.active and profile.status == "approved"
            and invite_h5_base()
        )
        return Response(
            {
                "data": {
                    "source": source_payload(source) if source else None,
                    "enabled": active,
                    "reward_amount": config.provider_reward_amount if config else 0,
                    "qr_data": qr_data(source) if active else "",
                    "summary": summary,
                    "items": [reward_payload(r) for r in rewards[(query - 1) * 20 : query * 20]],
                    "pagination": {"page": query, "page_size": 20, "total": rewards.count()},
                }
            }
        )
