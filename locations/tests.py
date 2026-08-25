from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from accounts.models import User

from .models import UserAddress


class LocationApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(phone="13800000901", password="test")
        self.client.force_login(self.user)

    @patch("locations.views.tencent_map.search")
    def test_search_places(self, search):
        search.return_value = [{
            "id": "poi-1", "name": "邯郸美乐城", "address": "人民东路456号",
            "city_name": "邯郸市", "district_name": "丛台区",
            "longitude": 114.512, "latitude": 36.613,
        }]
        response = self.client.get("/api/v1/locations/search/?keyword=美乐城")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["items"][0]["name"], "邯郸美乐城")
        search.assert_called_once_with("美乐城", "邯郸市")

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
