from rest_framework import serializers


class RechargeOrderInputSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=99)


class RechargePaymentSessionInputSerializer(serializers.Serializer):
    payment_scene = serializers.ChoiceField(
        choices=("official_account", "mobile_app"), default="official_account"
    )


class RechargeDiscountTierInputSerializer(serializers.Serializer):
    min_quantity = serializers.IntegerField(min_value=1, max_value=99)
    discount_rate_bps = serializers.IntegerField(min_value=1, max_value=10000)


class RechargeCampaignInputSerializer(serializers.Serializer):
    is_enabled = serializers.BooleanField(required=False)
    unit_face_amount = serializers.IntegerField(min_value=100000, max_value=100000, required=False)
    max_quantity_per_order = serializers.IntegerField(min_value=1, max_value=99, required=False)
    rules_text = serializers.CharField(max_length=500, allow_blank=True, required=False)
    tiers = RechargeDiscountTierInputSerializer(many=True, required=False)


class WalletPaginationSerializer(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)
