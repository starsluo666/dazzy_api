from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User

from .models import UserNotification
from .services import create_notification


class NotificationApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(phone="18820001111", nickname="通知用户")
        self.other = User.objects.create_user(phone="18820002222", nickname="其他用户")
        self.notification = create_notification(
            recipient=self.user,
            category=UserNotification.Category.SUPPORT,
            event_type=UserNotification.EventType.SUPPORT_REPLY,
            title="客服回复了你的工单",
            content="客服已开始核查相关记录。",
            target_type="support_case",
            target_id="SCNOTIFY0001",
            target_title="投诉 · 服务体验问题",
            action_text="查看工单进度",
            action_url="/pages/support/index?caseNo=SCNOTIFY0001",
            dedupe_key="test:notification:1",
        )
        read_notification = create_notification(
            recipient=self.user,
            category=UserNotification.Category.SYSTEM,
            event_type=UserNotification.EventType.SUPPORT_RESULT,
            title="系统通知",
            content="这是一条已读通知。",
            dedupe_key="test:notification:2",
        )
        UserNotification.objects.filter(pk=read_notification.pk).update(
            read_at=timezone.now()
        )

    def test_list_summary_filter_and_mark_read(self):
        self.client.force_authenticate(self.user)
        listing = self.client.get(reverse("notification-list"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["data"]["summary"]["total"], 2)
        self.assertEqual(listing.data["data"]["summary"]["unread"], 1)
        self.assertEqual(
            listing.data["data"]["summary"]["category_unread"]["support"], 1
        )

        filtered = self.client.get(
            reverse("notification-list"), {"category": "support"}
        )
        self.assertEqual(filtered.data["data"]["pagination"]["total"], 1)
        read_list = self.client.get(
            reverse("notification-list"), {"is_read": "true"}
        )
        self.assertEqual(read_list.data["data"]["pagination"]["total"], 1)
        self.assertTrue(read_list.data["data"]["items"][0]["is_read"])
        unread_list = self.client.get(
            reverse("notification-list"), {"is_read": "false"}
        )
        self.assertEqual(unread_list.data["data"]["pagination"]["total"], 1)
        self.assertFalse(unread_list.data["data"]["items"][0]["is_read"])

        marked = self.client.post(
            reverse("notification-read", args=(self.notification.public_id,))
        )
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(marked.data["data"]["is_read"])
        repeated = self.client.post(
            reverse("notification-read", args=(self.notification.public_id,))
        )
        self.assertEqual(repeated.status_code, 200)

    def test_notifications_are_private_and_read_all_is_scoped(self):
        other_notification = create_notification(
            recipient=self.other,
            category=UserNotification.Category.SUPPORT,
            event_type=UserNotification.EventType.SUPPORT_REPLY,
            title="其他用户通知",
            content="仅其他用户可见。",
            dedupe_key="test:notification:other",
        )
        self.client.force_authenticate(self.user)
        forbidden = self.client.post(
            reverse("notification-read", args=(other_notification.public_id,))
        )
        self.assertEqual(forbidden.status_code, 404)
        response = self.client.post(
            reverse("notification-read-all"), {"category": "support"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["updated"], 1)
        self.assertIsNone(
            UserNotification.objects.get(pk=other_notification.pk).read_at
        )

    def test_notification_creation_is_idempotent(self):
        duplicate = create_notification(
            recipient=self.user,
            category=UserNotification.Category.SUPPORT,
            event_type=UserNotification.EventType.SUPPORT_REPLY,
            title="不会重复创建",
            content="重复幂等键应返回原通知。",
            dedupe_key="test:notification:1",
        )
        self.assertEqual(duplicate.pk, self.notification.pk)
        self.assertEqual(UserNotification.objects.count(), 2)
