from django.test import TestCase


class HealthTests(TestCase):
    def test_health(self):
        response = self.client.get("/api/v1/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "ok")

    def test_readiness(self):
        response = self.client.get("/api/v1/health/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "ready")
