from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User, WechatLoginIdentity
from accounts.views import auth_payload
from orders.huifu import HuifuPaymentSessionResult

from .models import RechargeCampaign
from .services import create_recharge_order


@override_settings(
    WECHAT_OFFICIAL_ACCOUNT_APP_ID="wx-recharge",
    WECHAT_OFFICIAL_ACCOUNT_APP_SECRET="test-secret",
    WECHAT_OFFICIAL_ACCOUNT_OAUTH_CALLBACK_URL="https://api.example.test/callback/",
    WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL="https://app.example.test/#/pages/booking/payment",
)
class RechargeWechatPaymentTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(phone="13800007781", password=None)
        campaign = RechargeCampaign.current()
        campaign.is_enabled = True
        campaign.save()
        self.order = create_recharge_order(user_id=self.user.pk, quantity=1)
        self.base = f"/api/v1/wallet/recharge-orders/{self.order.order_no}"
        self.session = auth_payload(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.session['access']}")

    def authorize(self, openid="current-wechat"):
        response = self.client.get(self.base + "/payment-authorization/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["authorized"])
        state = parse_qs(urlsplit(response.data["data"]["authorize_url"]).query)["state"][0]
        with patch("orders.wechat_oauth._exchange_code", return_value=(openid, "")):
            callback = self.client.get(
                "/api/v1/payments/wechat/oauth/callback/",
                {
                    "code": "test-code",
                    "state": state,
                },
            )
        self.assertEqual(callback.status_code, 302)
        self.assertIn("/pages/wallet/recharge?", callback["Location"])

    @patch("wallets.services.get_huifu_payment_gateway")
    def test_fresh_session_authorization_is_required_and_payer_cannot_change(self, gateway):
        WechatLoginIdentity.objects.create(
            user=self.user,
            channel="official_account",
            app_id="wx-recharge",
            openid="historical-login-wechat",
            authorized_at=timezone.now(),
        )
        payload = {"payment_scene": "official_account"}
        self.assertEqual(
            self.client.post(self.base + "/payment-session/", payload).status_code, 400
        )
        gateway.return_value.create_payment.assert_not_called()
        self.authorize()
        other_session = auth_payload(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other_session['access']}")
        self.assertEqual(
            self.client.post(self.base + "/payment-session/", payload).status_code, 400
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.session['access']}")
        refreshed = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": self.session["refresh"]}, format="json",
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}")
        gateway.return_value.merchant_id = "test-merchant"
        gateway.return_value.create_payment.return_value = HuifuPaymentSessionResult(
            req_seq_id=self.order.order_no,
            req_date=timezone.localdate().strftime("%Y%m%d"),
            huifu_id="test-merchant",
            trade_type="T_JSAPI",
            trans_stat="P",
            hf_seq_id="test-trade",
            party_order_id="test-party",
            out_trans_id="",
            pay_info={"package": "prepay_id=test"},
            response_code="00000000",
            response_digest="a" * 64,
        )
        first = self.client.post(self.base + "/payment-session/", payload)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(
            gateway.return_value.create_payment.call_args.kwargs["sub_openid"], "current-wechat"
        )
        self.assertEqual(
            self.client.post(self.base + "/payment-session/", payload).status_code, 400
        )
        self.authorize()
        second = self.client.post(self.base + "/payment-session/", payload)
        self.assertEqual(second.status_code, 200, second.data)
        self.authorize("another-wechat")
        rejected = self.client.post(self.base + "/payment-session/", payload)
        self.assertEqual(rejected.status_code, 400, rejected.data)
        gateway.return_value.create_payment.assert_called_once()
        self.order.refresh_from_db()
        self.assertEqual(self.order.preorder_attempts, 1)
        self.assertEqual(len(self.order.wechat_payer_digest), 64)
