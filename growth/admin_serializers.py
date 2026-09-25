from django.db import transaction
from rest_framework import serializers

from orders.models import CouponTemplate

from .models import GrowthCampaignConfig, NewcomerGiftItem


class AdminGrowthConfigSerializer(serializers.Serializer):
    newcomer_gift_enabled = serializers.BooleanField(required=False)
    invitation_enabled = serializers.BooleanField(required=False)
    newcomer_gift_template_public_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=True, max_length=10
    )
    registration_reward_template_public_id = serializers.UUIDField(
        required=False, allow_null=True
    )
    first_order_reward_template_public_id = serializers.UUIDField(
        required=False, allow_null=True
    )

    def _template(self, public_id, field):
        if public_id is None:
            return None
        template = CouponTemplate.objects.filter(public_id=public_id).first()
        if template is None:
            raise serializers.ValidationError({field: "优惠券模板不存在。"})
        if not template.is_active:
            raise serializers.ValidationError({field: "只能选择启用中的优惠券模板。"})
        return template

    def validate(self, attrs):
        config = self.context.get("instance")
        current_gift_ids = (
            list(config.newcomer_gift_items.values_list("template__public_id", flat=True))
            if config else []
        )
        gift_ids = attrs.get("newcomer_gift_template_public_ids", current_gift_ids)
        if len(set(gift_ids)) != len(gift_ids):
            raise serializers.ValidationError(
                {"newcomer_gift_template_public_ids": "新人礼包不能重复选择同一模板。"}
            )
        gift_templates = list(CouponTemplate.objects.filter(public_id__in=gift_ids))
        if len(gift_templates) != len(gift_ids):
            raise serializers.ValidationError(
                {"newcomer_gift_template_public_ids": "部分优惠券模板不存在。"}
            )
        if any(not item.is_active for item in gift_templates):
            raise serializers.ValidationError(
                {"newcomer_gift_template_public_ids": "新人礼包只能使用启用中的优惠券模板。"}
            )

        registration = (
            self._template(
                attrs["registration_reward_template_public_id"],
                "registration_reward_template_public_id",
            )
            if "registration_reward_template_public_id" in attrs
            else (config.registration_reward_template if config else None)
        )
        first_order = (
            self._template(
                attrs["first_order_reward_template_public_id"],
                "first_order_reward_template_public_id",
            )
            if "first_order_reward_template_public_id" in attrs
            else (config.first_order_reward_template if config else None)
        )
        newcomer_enabled = attrs.get(
            "newcomer_gift_enabled", config.newcomer_gift_enabled if config else False
        )
        invitation_enabled = attrs.get(
            "invitation_enabled", config.invitation_enabled if config else False
        )
        if newcomer_enabled and not gift_ids:
            raise serializers.ValidationError(
                {"newcomer_gift_template_public_ids": "启用新人礼包前至少选择一张优惠券。"}
            )
        if invitation_enabled and not registration:
            raise serializers.ValidationError(
                {"registration_reward_template_public_id": "启用邀请奖励前请选择注册奖励券。"}
            )
        if invitation_enabled and not first_order:
            raise serializers.ValidationError(
                {"first_order_reward_template_public_id": "启用邀请奖励前请选择首单奖励券。"}
            )
        attrs["_gift_ids"] = gift_ids
        attrs["_gift_templates"] = {str(item.public_id): item for item in gift_templates}
        attrs["_registration_template"] = registration
        attrs["_first_order_template"] = first_order
        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        config = self.context.get("instance")
        if config is None:
            config, _ = GrowthCampaignConfig.objects.get_or_create(pk=1)
        data = self.validated_data
        for field in ("newcomer_gift_enabled", "invitation_enabled"):
            if field in data:
                setattr(config, field, data[field])
        config.registration_reward_template = data["_registration_template"]
        config.first_order_reward_template = data["_first_order_template"]
        config.updated_by = kwargs.get("updated_by")
        config.save()

        if "newcomer_gift_template_public_ids" in data:
            config.newcomer_gift_items.all().delete()
            templates = data["_gift_templates"]
            NewcomerGiftItem.objects.bulk_create(
                [
                    NewcomerGiftItem(
                        config=config,
                        template=templates[str(public_id)],
                        position=position,
                    )
                    for position, public_id in enumerate(data["_gift_ids"])
                ]
            )
        return config


class AdminInvitationQuerySerializer(serializers.Serializer):
    search = serializers.CharField(required=False, allow_blank=True, max_length=50)
    status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=("registered", "first_order_rewarded"),
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=100)
