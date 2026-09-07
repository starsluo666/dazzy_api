from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from accounts.models import User

from .models import UserAddress
from .tencent import TencentMapClient


class LocationApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(phone="13800000901", password="test")
        self.client.force_login(self.user)

    @patch("locations.views.tencent_map.search")
    def test_search_places(self, search):
        search.return_value = [{
            "id": "poi-1", "name": "邯郸美乐城", "address": "人民东路456号",
            "city_name": "邯郸市", "city_code": "130400", "district_name": "丛台区",
            "longitude": 114.512, "latitude": 36.613,
        }]
        response = self.client.get("/api/v1/locations/search/?keyword=美乐城")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["items"][0]["name"], "邯郸美乐城")
        self.assertEqual(response.json()["data"]["items"][0]["city_code"], "130400")
        search.assert_called_once_with("美乐城", "邯郸市")

    @patch("config.throttles.MapProxyBurstThrottle.rate", "2/min", create=True)
    @patch("locations.views.tencent_map.search", return_value=[])
    def test_map_proxy_burst_limit(self, search):
        for _ in range(2):
            response = self.client.get("/api/v1/locations/search/?keyword=美乐城")
            self.assertEqual(response.status_code, 200)

        limited = self.client.get("/api/v1/locations/search/?keyword=美乐城")

        self.assertEqual(limited.status_code, 429)
        self.assertEqual(search.call_count, 2)

    @patch("config.throttles.MapProxyDailyThrottle.rate", "2/day", create=True)
    @patch("locations.views.tencent_map.search", return_value=[])
    def test_map_proxy_daily_limit(self, search):
        for _ in range(2):
            response = self.client.get("/api/v1/locations/search/?keyword=美乐城")
            self.assertEqual(response.status_code, 200)

        limited = self.client.get("/api/v1/locations/search/?keyword=美乐城")

        self.assertEqual(limited.status_code, 429)
        self.assertEqual(search.call_count, 2)

    def test_reverse_geocode_rejects_missing_or_invalid_coordinates(self):
        missing = self.client.get("/api/v1/locations/reverse-geocode/")
        invalid = self.client.get(
            "/api/v1/locations/reverse-geocode/",
            {"longitude": "invalid", "latitude": "36.613"},
        )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(invalid.status_code, 400)

    def test_create_and_list_saved_address(self):
        payload = {
            "name": "邯郸美乐城", "address": "人民东路456号", "city_name": "邯郸市",
            "contact_name": "张三", "contact_gender": "mr", "contact_phone": "13812346688",
            "longitude": "114.5120000", "latitude": "36.6130000", "is_default": True,
        }
        created = self.client.post("/api/v1/addresses/", payload, content_type="application/json")
        self.assertEqual(created.status_code, 201)
        listed = self.client.get("/api/v1/addresses/")
        item = listed.json()["data"]["items"][0]
        self.assertEqual(Decimal(item["longitude"]), Decimal("114.5120000"))
        self.assertEqual(item["contact_gender_label"], "先生")
        self.assertTrue(item["is_default"])

    def test_first_address_defaults_and_update_moves_default(self):
        first = self.client.post(
            "/api/v1/addresses/",
            {
                "name": "美乐城", "address": "人民东路456号", "city_name": "邯郸市",
                "contact_name": "张三", "contact_gender": "mr", "contact_phone": "13812346688",
                "longitude": "114.5120000", "latitude": "36.6130000",
            },
            content_type="application/json",
        )
        second = self.client.post(
            "/api/v1/addresses/",
            {
                "name": "博物馆", "address": "中华北大街45号", "city_name": "邯郸市",
                "contact_name": "李女士", "contact_gender": "ms", "contact_phone": "13912346688",
                "longitude": "114.5010000", "latitude": "36.6100000",
            },
            content_type="application/json",
        )
        self.assertTrue(first.json()["data"]["is_default"])
        self.assertFalse(second.json()["data"]["is_default"])

        changed = self.client.patch(
            f"/api/v1/addresses/{second.json()['data']['id']}/",
            {"is_default": True},
            content_type="application/json",
        )
        self.assertEqual(changed.status_code, 200)
        listed = self.client.get("/api/v1/addresses/").json()["data"]["items"]
        self.assertEqual(sum(item["is_default"] for item in listed), 1)
        self.assertEqual(listed[0]["id"], second.json()["data"]["id"])

    def test_delete_default_promotes_remaining_address(self):
        first = UserAddress.objects.create(
            user=self.user, name="默认地址", address="地址1", city_name="邯郸市",
            longitude="114.5120000", latitude="36.6130000", is_default=True,
        )
        second = UserAddress.objects.create(
            user=self.user, name="备用地址", address="地址2", city_name="邯郸市",
            longitude="114.5020000", latitude="36.6030000",
        )

        response = self.client.delete(f"/api/v1/addresses/{first.id}/")

        self.assertEqual(response.status_code, 204)
        second.refresh_from_db()
        self.assertTrue(second.is_default)

    def test_create_requires_complete_contact_information(self):
        response = self.client.post(
            "/api/v1/addresses/",
            {
                "name": "美乐城", "address": "人民东路456号", "city_name": "邯郸市",
                "longitude": "114.5120000", "latitude": "36.6130000",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("contact_name", str(response.json()))


class TencentMapClientTests(TestCase):
    def setUp(self):
        self.map_client = TencentMapClient()

    @patch.object(TencentMapClient, "_get")
    def test_search_returns_prefecture_city_code(self, get):
        get.return_value = {
            "data": [{
                "id": "poi-shenzhen",
                "title": "深圳湾公园",
                "address": "滨海大道",
                "city": "深圳市",
                "district": "南山区",
                "adcode": "440305",
                "location": {"lng": 113.951, "lat": 22.526},
            }]
        }

        item = self.map_client.search("深圳湾公园", "深圳市")[0]

        self.assertEqual(item["city_code"], "440300")

    @patch.object(TencentMapClient, "_get")
    def test_reverse_geocode_returns_municipality_city_code(self, get):
        get.return_value = {
            "result": {
                "address": "北京市朝阳区建国路",
                "formatted_addresses": {"recommend": "国贸"},
                "address_component": {"city": "北京市", "district": "朝阳区"},
                "ad_info": {"adcode": "110105"},
                "location": {"lng": 116.46, "lat": 39.91},
            }
        }

        item = self.map_client.reverse_geocode(Decimal("116.46"), Decimal("39.91"))

        self.assertEqual(item["city_code"], "110100")
