from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from accounts.models import User


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
            "longitude": "114.5120000", "latitude": "36.6130000", "is_default": True,
        }
        created = self.client.post("/api/v1/addresses/", payload, content_type="application/json")
        self.assertEqual(created.status_code, 201)
        listed = self.client.get("/api/v1/addresses/")
        item = listed.json()["data"]["items"][0]
        self.assertEqual(Decimal(item["longitude"]), Decimal("114.5120000"))
        self.assertTrue(item["is_default"])
