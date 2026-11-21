import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.http import Http404
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from accounts.wechat_js_sdk import navigation_config

from .models import ProviderOrder
from .serializers import CustomerProviderOrderDetailSerializer, ProviderOrderSerializer


class CustomerOrderContactTests(SimpleTestCase):
    def test_contact_details_are_scoped_to_paid_service(self):
        serializer = CustomerProviderOrderDetailSerializer()
        phone = "13800000102"
        order = SimpleNamespace(
            provider=SimpleNamespace(user=SimpleNamespace(phone=phone)),
            paid_at=datetime.now(timezone.utc),
        )
        active_statuses = {
            ProviderOrder.Status.PENDING_SERVICE, ProviderOrder.Status.DEPARTED,
            ProviderOrder.Status.IN_SERVICE, ProviderOrder.Status.PENDING_CONFIRMATION,
        }
        for status in ProviderOrder.Status:
            with self.subTest(status=status):
                order.status = status
                expected = phone if status in active_statuses else None
                self.assertEqual(serializer.get_provider_contact_phone(order), expected)

        order.status = ProviderOrder.Status.PENDING_SERVICE
        order.paid_at = None
        self.assertIsNone(serializer.get_provider_contact_phone(order))
        self.assertNotIn("provider_contact_phone", ProviderOrderSerializer().fields)


@override_settings(
    WECHAT_OFFICIAL_ACCOUNT_APP_ID="wx-navigation-test",
    WECHAT_OFFICIAL_ACCOUNT_APP_SECRET="test-only-secret",
    WECHAT_H5_LOGIN_RETURN_URL="https://app.example.test/#/pages/index/index",
    WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL="",
    PROVIDER_INVITE_H5_BASE_URL="",
)
class CustomerOrderNavigationTests(SimpleTestCase):
    path = "/api/v1/provider-orders/DZYTEST/navigation/"
    page_url = "https://app.example.test/?from=official"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @patch("accounts.wechat_js_sdk._read_json")
    def test_tickets_are_cached_and_signature_uses_exact_page_url(self, read_json):
        read_json.side_effect = [
            {"access_token": "token-only-for-test", "expires_in": 7200},
            {"ticket": "ticket-only-for-test", "expires_in": 7200},
        ]
        first = navigation_config(self.page_url)
        second = navigation_config(self.page_url)
        self.assertEqual(read_json.call_count, 2)
        for config in (first, second):
            signature_input = (
                f"jsapi_ticket=ticket-only-for-test&noncestr={config['nonceStr']}"
                f"&timestamp={config['timestamp']}&url={self.page_url}"
            )
            self.assertEqual(config["appId"], "wx-navigation-test")
            self.assertEqual(config["signature"], hashlib.sha1(signature_input.encode()).hexdigest())
            self.assertEqual(set(config), {"appId", "nonceStr", "timestamp", "signature"})

    @patch("orders.views.get_object_or_404")
    @patch("accounts.wechat_js_sdk._read_json")
    def test_navigation_api_checks_order_ownership_and_page_origin(self, read_json, get_order):
        callback = resolve(self.path).func
        user = User(pk=87, phone="13800000101")
        request = APIRequestFactory().get(self.path, {"url": self.page_url})
        force_authenticate(request, user=user)
        get_order.side_effect = Http404
        response = callback(request, order_no="DZYTEST")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(get_order.call_args.kwargs, {"order_no": "DZYTEST", "customer": user})

        get_order.side_effect = None
        for page_url in ("https://other.example.test/", f"{self.page_url}#/pages/orders/detail"):
            with self.subTest(url=page_url):
                request = APIRequestFactory().get(self.path, {"url": page_url})
                force_authenticate(request, user=user)
                self.assertEqual(callback(request, order_no="DZYTEST").status_code, 400)
        read_json.assert_not_called()
