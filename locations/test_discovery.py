from unittest.mock import patch

from django.core.cache import cache
from rest_framework.test import APITestCase

from accounts.models import User
from backoffice.models import AdminAuditLog, PlatformOperationSetting
from config.geospatial import gcj02_to_wgs84, wgs84_to_gcj02
from locations.discovery import default_discovery_cities


class DiscoveryCityTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_guest_reads_only_open_cities_without_creating_settings(self):
        response = self.client.get('/api/v1/locations/cities/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['items'], [
            {'city_code': '130400', 'city_name': '邯郸市'},
        ])
        self.assertFalse(PlatformOperationSetting.objects.exists())

    def test_new_settings_default_to_handan_only(self):
        settings = PlatformOperationSetting.objects.create()
        settings.refresh_from_db()
        expected = [{'city_code': '130400', 'city_name': '邯郸市'}]
        self.assertEqual(settings.discovery_cities, expected)
        self.assertEqual(self.client.get('/api/v1/locations/cities/').data['data']['items'], expected)

    def test_admin_configures_cities_with_audit_and_public_api_follows(self):
        admin = User.objects.create_superuser(phone='19900009871', password='test')
        self.client.force_authenticate(admin)
        payload = [{'city_code': '510100', 'city_name': '成都市'}]
        response = self.client.patch('/api/v1/admin/operation-settings/platform/',
                                     {'discovery_cities': payload}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['discovery_cities'], payload)
        audit = AdminAuditLog.objects.get(action='operations.platform.update')
        self.assertEqual(audit.after['discovery_cities'], payload)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/v1/locations/cities/').data['data']['items'], payload)

    def test_regular_user_cannot_configure_cities(self):
        user = User.objects.create_user(phone='19900009872', password='test')
        self.client.force_authenticate(user)
        response = self.client.patch('/api/v1/admin/operation-settings/platform/',
                                     {'discovery_cities': default_discovery_cities()}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_invalid_city_configuration_is_rejected(self):
        admin = User.objects.create_superuser(phone='19900009873', password='test')
        self.client.force_authenticate(admin)
        invalid_values = [[], [{'city_code': '123', 'city_name': '测试'}],
                          [{'city_code': '130400', 'city_name': ' '}],
                          [default_discovery_cities()[0]] * 2,
                          [{'city_code': '130400', 'city_name': '邯郸市'},
                           {'city_code': '130500', 'city_name': '邯郸市'}]]
        for value in invalid_values:
            with self.subTest(value=value):
                response = self.client.patch('/api/v1/admin/operation-settings/platform/',
                                             {'discovery_cities': value}, format='json')
                self.assertEqual(response.status_code, 400)
        self.assertFalse(AdminAuditLog.objects.exists())

    @patch('locations.views.tencent_map.reverse_geocode')
    def test_guest_location_converts_gps_and_returns_no_street_address(self, reverse):
        reverse.return_value = {'city_code': '130400', 'city_name': '邯郸市', 'address': 'private'}
        response = self.client.post('/api/v1/locations/locate/',
                                    {'longitude': '114.5180000', 'latitude': '36.6070000'}, format='json')
        self.assertEqual(response.status_code, 200)
        data = response.data['data']
        self.assertTrue(data['is_open'])
        self.assertNotIn('address', data)
        lng, lat = wgs84_to_gcj02(114.518, 36.607)
        reverse.assert_called_once_with(f'{lng:.7f}', f'{lat:.7f}')
        self.assertEqual(data['longitude'], f'{lng:.7f}')
        self.assertEqual(response['Cache-Control'], 'no-store')

    @patch('locations.views.tencent_map.reverse_geocode')
    def test_unopened_city_is_not_silently_replaced(self, reverse):
        reverse.return_value = {'city_code': '510100', 'city_name': '成都市'}
        response = self.client.post('/api/v1/locations/locate/',
                                    {'longitude': '104.06', 'latitude': '30.67'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['data']['is_open'])
        self.assertEqual(response.data['data']['city_code'], '510100')

    @patch('locations.views.tencent_map.reverse_geocode')
    def test_invalid_coordinates_never_call_map(self, reverse):
        for payload in ({}, {'longitude': 181, 'latitude': 36}, {'longitude': 114, 'latitude': 91}):
            response = self.client.post('/api/v1/locations/locate/', payload, format='json')
            self.assertEqual(response.status_code, 400)
        reverse.assert_not_called()

    @patch('config.throttles.MapProxyBurstThrottle.rate', '2/min', create=True)
    @patch('locations.views.tencent_map.reverse_geocode', return_value={'city_code': '130400', 'city_name': '邯郸市'})
    def test_guest_location_is_ip_throttled(self, reverse):
        for expected in (200, 200, 429):
            response = self.client.post('/api/v1/locations/locate/',
                                        {'longitude': '114.518', 'latitude': '36.607'}, format='json')
            self.assertEqual(response.status_code, expected)
        self.assertEqual(reverse.call_count, 2)

    def test_coordinate_conversion_round_trip(self):
        for lng, lat in ((114.518, 36.607), (116.397, 39.908), (-73.98, 40.75)):
            converted = wgs84_to_gcj02(lng, lat)
            original = gcj02_to_wgs84(*converted)
            self.assertAlmostEqual(original.x, lng, places=4)
            self.assertAlmostEqual(original.y, lat, places=4)
