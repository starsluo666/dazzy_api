from django.test import TestCase

from .models import User


class UserModelTests(TestCase):
    def test_create_user_with_phone(self):
        user = User.objects.create_user(phone="13800000000", password="test-password")
        self.assertEqual(user.phone, "13800000000")
        self.assertTrue(user.check_password("test-password"))
        self.assertEqual(user.verification_status, User.VerificationStatus.UNVERIFIED)
