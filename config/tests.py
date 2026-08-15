from decimal import Decimal

from django.test import SimpleTestCase

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
