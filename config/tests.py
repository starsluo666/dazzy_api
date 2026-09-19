from decimal import Decimal

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.settings import api_settings

from .geospatial import gcj02_to_wgs84


class GeospatialTests(SimpleTestCase):
    def test_gcj02_to_wgs84_keeps_srid_and_changes_china_coordinate(self):
        point = gcj02_to_wgs84(Decimal("116.4039810"), Decimal("39.9150010"))

        self.assertEqual(point.srid, 4326)
        self.assertNotAlmostEqual(point.x, 116.4039810, places=4)
        self.assertNotAlmostEqual(point.y, 39.9150010, places=4)

    def test_gcj02_to_wgs84_does_not_transform_overseas_coordinate(self):
        point = gcj02_to_wgs84(-122.4194, 37.7749)

        self.assertEqual(point.x, -122.4194)
        self.assertEqual(point.y, 37.7749)


class ApiSecurityDefaultsTests(SimpleTestCase):
    def test_unauthenticated_access_is_denied_by_default(self):
        self.assertEqual(api_settings.DEFAULT_PERMISSION_CLASSES, [IsAuthenticated])

    def test_api_documentation_is_not_accessible_outside_debug(self):
        self.assertFalse(settings.DEBUG)
        self.assertIn(self.client.get("/api/docs/").status_code, (401, 403, 404))


class PaymentCapabilityTests(SimpleTestCase):
    @override_settings(DEBUG=False, HUIFU_PAYMENT_ENABLED=False)
    def test_production_does_not_advertise_unimplemented_payment_paths(self):
        response = self.client.get("/api/v1/payments/capabilities/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["provider_order"]["mobile_app"]["available"])
        self.assertFalse(data["activity_publish"]["real"]["available"])
        self.assertFalse(data["activity_publish"]["mock"]["available"])
        self.assertFalse(data["activity_participation"]["real"]["available"])

    @override_settings(DEBUG=True, HUIFU_PAYMENT_ENABLED=False)
    def test_local_environment_advertises_only_mock_payment(self):
        response = self.client.get("/api/v1/payments/capabilities/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["provider_order"]["mock"]["available"])
        self.assertTrue(data["activity_publish"]["mock"]["available"])
        self.assertTrue(data["activity_participation"]["mock"]["available"])
        self.assertFalse(data["provider_order"]["official_account"]["available"])
