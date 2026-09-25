from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import json
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError as DRFValidationError

from accounts.models import User
from backoffice.models import PlatformOperationSetting
from mediafiles.models import MediaAsset
from notifications.models import UserNotification
from orders.huifu import (
    HuifuCloseResult,
    HuifuPaymentQueryResult,
    HuifuPaymentSessionResult,
    HuifuRefundQueryResult,
    HuifuRefundResult,
)
from taskcenter.models import ScheduledTask
from taskcenter.services import process_due_tasks
from wallets.models import UserWallet, WalletPaymentAllocation

from .models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityCategory,
    ActivityHuifuPaymentOrder,
    ActivityHuifuNotification,
    ActivityHuifuRefundOrder,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivitySettlement,
)
from .payment_gateway import PaymentResult
from .services import (
    calculate_publish_service_fee,
    create_activity_participation_refund,
    expire_activity_participation_payment,
    get_or_create_participation_order,
    process_activity_timeouts,
)
from .huifu import (
    confirm_activity_huifu_payment_status,
    create_activity_huifu_payment_session,
    process_activity_huifu_cancel_compensation,
)


class ActivityModelTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(phone="13800000002", password="test-password")
        self.category = ActivityCategory.objects.create(name="台球", slug="billiards")
        self.cover = MediaAsset.objects.create(
            owner=self.organizer,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.ACTIVITY_COVER,
            status=MediaAsset.Status.UPLOADED,
            object_key="dazzy-test/public/activity-covers/test.webp",
        )

    def build_activity(self, **overrides):
        starts_at = timezone.now() + timedelta(days=3)
        values = {
            "organizer": self.organizer,
            "category": self.category,
            "title": "周末台球局",
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(hours=3),
            "formation_deadline": starts_at - timedelta(hours=12),
            "meeting_place_name": "测试台球俱乐部",
            "meeting_address": "北京市测试地址",
            "source_longitude": Decimal("116.4039810"),
            "source_latitude": Decimal("39.9150010"),
            "meeting_point": Point(116.397755, 39.913873, srid=4326),
            "capacity": 6,
            "min_participants": 4,
            "description": "一起打球",
            "participation_rules": "准时到场",
            "aa_principal_amount": 4800,
            "refund_template_version": "standard-v1",
            "cover_id": str(self.cover.pk),
            "refund_rule_snapshot": {"version": "standard-v1"},
        }
        values.update(overrides)
        return Activity(**values)

    def test_activity_accepts_valid_invariants(self):
        activity = self.build_activity()
        activity.full_clean()
        activity.save()

        self.assertEqual(activity.aa_principal_amount, 4800)
        self.assertEqual(activity.meeting_point.srid, 4326)

    def test_publish_service_fee_uses_half_up_rounding(self):
        self.assertEqual(calculate_publish_service_fee(6805), 681)

    @override_settings(DEBUG=True)
    @patch("config.payment_capabilities.ensure_activity_real_payment_available")
    @patch("activities.huifu.get_huifu_payment_gateway")
    def test_activity_huifu_session_is_server_priced_and_idempotent(
        self, gateway, _ensure_real_payment
    ):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000101",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        _participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        gateway.return_value.merchant_id = "6666000000000000"
        gateway.return_value.create_payment.return_value = HuifuPaymentSessionResult(
            req_seq_id=order.order_no,
            req_date=timezone.localdate().strftime("%Y%m%d"),
            huifu_id="6666000000000000",
            trade_type="T_JSAPI",
            trans_stat="P",
            hf_seq_id="HF-ACTIVITY-SESSION",
            party_order_id="PARTY-ACTIVITY",
            out_trans_id="",
            pay_info={"package": "prepay_id=ACTIVITY"},
            response_code="00000000",
            response_digest="a" * 64,
        )

        first, created = create_activity_huifu_payment_session(
            payment_kind="activity_participation",
            activity_id=activity.pk,
            user_id=participant.pk,
            payment_scene="official_account",
            sub_openid="participant-openid",
        )
        second, replay_created = create_activity_huifu_payment_session(
            payment_kind="activity_participation",
            activity_id=activity.pk,
            user_id=participant.pk,
            payment_scene="official_account",
            sub_openid="participant-openid",
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.pay_info, second.pay_info)
        gateway.return_value.create_payment.assert_called_once()
        call = gateway.return_value.create_payment.call_args.kwargs
        self.assertEqual(call["amount"], order.payable_amount)
        self.assertEqual(call["attach"], f"activity-participation:{order.order_no}")
        self.assertEqual(call["sub_openid"], "participant-openid")

    @override_settings(DEBUG=True)
    @patch("config.payment_capabilities.ensure_activity_real_payment_available")
    @patch("activities.huifu.get_huifu_payment_gateway")
    def test_activity_full_balance_payment_bypasses_huifu_and_consumes_wallet(
        self, gateway, ensure_real_payment
    ):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000121",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        wallet = UserWallet.objects.create(
            user=participant,
            available_balance=order.payable_amount + 500,
        )

        result, created = create_activity_huifu_payment_session(
            payment_kind="activity_participation",
            activity_id=activity.pk,
            user_id=participant.pk,
            payment_scene="official_account",
            sub_openid="",
        )

        participation.refresh_from_db()
        order.refresh_from_db()
        wallet.refresh_from_db()
        allocation = WalletPaymentAllocation.objects.get(
            business_type=WalletPaymentAllocation.BusinessType.ACTIVITY_PARTICIPATION,
            business_order_no=order.order_no,
        )
        self.assertTrue(created)
        self.assertEqual(result.trade_type, "BALANCE")
        self.assertEqual(result.external_amount, 0)
        self.assertEqual(participation.status, ActivityParticipation.Status.ACTIVE)
        self.assertEqual(order.channel, ActivityParticipationPaymentOrder.Channel.BALANCE)
        self.assertEqual(allocation.status, WalletPaymentAllocation.Status.CONSUMED)
        self.assertEqual(wallet.available_balance, 500)
        self.assertEqual(wallet.frozen_balance, 0)
        ensure_real_payment.assert_not_called()
        gateway.assert_not_called()

    @override_settings(DEBUG=True)
    @patch("config.payment_capabilities.ensure_activity_real_payment_available")
    @patch("activities.huifu.get_huifu_payment_gateway")
    def test_activity_mixed_payment_only_sends_external_remainder_to_huifu(
        self, gateway, _ensure_real_payment
    ):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000122",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        _participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        UserWallet.objects.create(user=participant, available_balance=2_000)
        gateway.return_value.merchant_id = "6666000000000000"
        gateway.return_value.create_payment.return_value = HuifuPaymentSessionResult(
            req_seq_id=order.order_no,
            req_date=timezone.localdate().strftime("%Y%m%d"),
            huifu_id="6666000000000000",
            trade_type="T_JSAPI",
            trans_stat="P",
            hf_seq_id="HF-ACTIVITY-MIXED",
            party_order_id="PARTY-ACTIVITY-MIXED",
            out_trans_id="",
            pay_info={"package": "prepay_id=ACTIVITY-MIXED"},
            response_code="00000000",
            response_digest="b" * 64,
        )

        result, created = create_activity_huifu_payment_session(
            payment_kind="activity_participation",
            activity_id=activity.pk,
            user_id=participant.pk,
            payment_scene="official_account",
            sub_openid="participant-openid",
        )

        self.assertTrue(created)
        self.assertEqual(result.wallet_amount, 2_000)
        self.assertEqual(result.external_amount, order.payable_amount - 2_000)
        self.assertEqual(
            gateway.return_value.create_payment.call_args.kwargs["amount"],
            order.payable_amount - 2_000,
        )

    @override_settings(DEBUG=True)
    def test_activity_balance_refund_returns_to_wallet_without_gateway(self):
        from .services import process_activity_participation_refund

        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000123",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        wallet = UserWallet.objects.create(
            user=participant, available_balance=order.payable_amount
        )
        create_activity_huifu_payment_session(
            payment_kind="activity_participation",
            activity_id=activity.pk,
            user_id=participant.pk,
            payment_scene="official_account",
            sub_openid="",
        )
        refund, created = create_activity_participation_refund(
            participation=participation,
            payment_order=order,
            refund_type=ActivityParticipationRefundOrder.RefundType.ADMIN_CANCELLATION,
            idempotency_key=f"test-balance-refund:{order.order_no}",
            principal_refund_amount=order.aa_principal_amount,
            service_fee_refund_amount=order.platform_service_fee_amount,
            reason="测试余额退款",
        )

        completed, changed = process_activity_participation_refund(refund.refund_no)

        wallet.refresh_from_db()
        order.refresh_from_db()
        self.assertTrue(created)
        self.assertTrue(changed)
        self.assertEqual(refund.wallet_refund_amount, order.payable_amount)
        self.assertEqual(refund.external_refund_amount, 0)
        self.assertEqual(completed.status, ActivityParticipationRefundOrder.Status.SUCCEEDED)
        self.assertEqual(order.status, ActivityParticipationPaymentOrder.Status.REFUNDED)
        self.assertEqual(wallet.available_balance, order.payable_amount)

    @override_settings(DEBUG=True)
    @patch("activities.huifu.get_huifu_payment_gateway")
    def test_activity_huifu_active_query_applies_participation_once(self, gateway):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000102",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        payment = ActivityHuifuPaymentOrder.objects.create(
            participation_order=order,
            preorder_status=ActivityHuifuPaymentOrder.PreorderStatus.READY,
            gateway_merchant_id="6666000000000000",
            req_date="20260919",
            req_seq_id=order.order_no,
            payment_scene="official_account",
            trade_type="T_JSAPI",
            gateway_trade_no="HF-ACTIVITY-PAID",
        )
        gateway.return_value.query_payment.return_value = HuifuPaymentQueryResult(
            req_date=payment.req_date,
            req_seq_id=payment.req_seq_id,
            huifu_id=payment.gateway_merchant_id,
            trans_stat="S",
            trans_amt=f"{order.payable_amount / 100:.2f}",
            end_time=timezone.localtime().strftime("%Y%m%d%H%M%S"),
            trade_type="T_JSAPI",
            gateway_trade_no=payment.gateway_trade_no,
            party_order_id="PARTY-ACTIVITY-PAID",
            out_trans_id="OUT-ACTIVITY-PAID",
            response_code="00000000",
            response_digest="b" * 64,
        )

        first = confirm_activity_huifu_payment_status(
            payment_kind="activity_participation",
            activity_id=activity.pk,
            user_id=participant.pk,
        )
        second = confirm_activity_huifu_payment_status(
            payment_kind="activity_participation",
            activity_id=activity.pk,
            user_id=participant.pk,
        )

        self.assertEqual(first["state"], "paid")
        self.assertEqual(second["state"], "paid")
        gateway.return_value.query_payment.assert_called_once()
        order.refresh_from_db()
        participation.refresh_from_db()
        self.assertEqual(order.status, ActivityParticipationPaymentOrder.Status.PAID)
        self.assertEqual(participation.status, ActivityParticipation.Status.ACTIVE)

    @override_settings(DEBUG=True)
    @patch("activities.huifu.get_huifu_payment_gateway")
    def test_activity_cancel_compensation_queries_then_closes_unpaid_trade(
        self, gateway
    ):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000105",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        payment = ActivityHuifuPaymentOrder.objects.create(
            participation_order=order,
            preorder_status=ActivityHuifuPaymentOrder.PreorderStatus.READY,
            gateway_merchant_id="6666000000000000",
            req_date="20260919",
            req_seq_id=order.order_no,
            payment_scene="official_account",
            trade_type="T_JSAPI",
            preorder_requested_at=order.expires_at - timedelta(minutes=2),
        )
        gateway.return_value.query_payment.return_value = HuifuPaymentQueryResult(
            req_date=payment.req_date,
            req_seq_id=payment.req_seq_id,
            huifu_id=payment.gateway_merchant_id,
            trans_stat="P",
            trans_amt=f"{order.payable_amount / 100:.2f}",
            end_time="",
            trade_type="T_JSAPI",
            gateway_trade_no="",
            party_order_id="",
            out_trans_id="",
            response_code="00000000",
            response_digest="f" * 64,
        )
        gateway.return_value.close_payment.return_value = HuifuCloseResult(
            req_date=timezone.localtime(order.expires_at).strftime("%Y%m%d"),
            req_seq_id=f"{order.order_no}CLOSE",
            huifu_id=payment.gateway_merchant_id,
            trans_stat="S",
            org_trans_stat="F",
            response_code="00000000",
            response_digest="1" * 64,
        )

        expired = process_due_tasks(
            task_types=[ScheduledTask.Type.ACTIVITY_PARTICIPATION_PAYMENT_EXPIRY],
            now=order.expires_at,
        )
        processed = process_due_tasks(
            task_types=[
                ScheduledTask.Type.ACTIVITY_PARTICIPATION_CANCEL_COMPENSATION
            ],
            now=order.expires_at,
        )

        self.assertEqual(expired["succeeded"], 1)
        self.assertEqual(processed["succeeded"], 1)
        participation.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(participation.status, ActivityParticipation.Status.EXPIRED)
        self.assertEqual(payment.gateway_close_status, "S")
        self.assertTrue(payment.close_req_seq_id.endswith("CLOSE"))
        self.assertEqual(gateway.return_value.query_payment.call_count, 2)
        gateway.return_value.close_payment.assert_called_once()

    @override_settings(DEBUG=True)
    @patch("activities.huifu.get_huifu_payment_gateway")
    def test_activity_cancel_compensation_refunds_late_success(self, gateway):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000106",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        payment = ActivityHuifuPaymentOrder.objects.create(
            participation_order=order,
            preorder_status=ActivityHuifuPaymentOrder.PreorderStatus.READY,
            gateway_merchant_id="6666000000000000",
            req_date="20260919",
            req_seq_id=order.order_no,
            payment_scene="official_account",
            trade_type="T_JSAPI",
            preorder_requested_at=order.expires_at - timedelta(minutes=2),
        )
        paid_at = order.expires_at + timedelta(seconds=1)
        gateway.return_value.query_payment.return_value = HuifuPaymentQueryResult(
            req_date=payment.req_date,
            req_seq_id=payment.req_seq_id,
            huifu_id=payment.gateway_merchant_id,
            trans_stat="S",
            trans_amt=f"{order.payable_amount / 100:.2f}",
            end_time=timezone.localtime(paid_at).strftime("%Y%m%d%H%M%S"),
            trade_type="T_JSAPI",
            gateway_trade_no="HF-ACTIVITY-LATE-SUCCESS",
            party_order_id="PARTY-ACTIVITY-LATE",
            out_trans_id="OUT-ACTIVITY-LATE",
            response_code="00000000",
            response_digest="2" * 64,
        )
        expire_activity_participation_payment(
            order_no=order.order_no,
            now=order.expires_at,
        )

        outcome = process_activity_huifu_cancel_compensation(
            payment_kind="activity_participation",
            order_no=order.order_no,
            now=paid_at,
        )

        order.refresh_from_db()
        participation.refresh_from_db()
        refund = ActivityParticipationRefundOrder.objects.get(
            payment_order=order,
            refund_type=ActivityParticipationRefundOrder.RefundType.PAYMENT_TIMEOUT,
        )
        self.assertEqual(outcome["state"], "refund_processing")
        self.assertEqual(order.status, ActivityParticipationPaymentOrder.Status.PAID)
        self.assertEqual(participation.status, ActivityParticipation.Status.EXPIRED)
        self.assertEqual(refund.refund_amount, order.payable_amount)
        self.assertTrue(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
                business_key=refund.refund_no,
            ).exists()
        )
        gateway.return_value.close_payment.assert_not_called()

    @override_settings(DEBUG=True)
    @patch("activities.huifu.get_huifu_payment_gateway")
    @patch("orders.services.get_huifu_payment_gateway")
    def test_activity_huifu_notification_is_verified_queried_and_idempotent(
        self, notification_gateway, activity_gateway
    ):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000104",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        payment = ActivityHuifuPaymentOrder.objects.create(
            participation_order=order,
            preorder_status=ActivityHuifuPaymentOrder.PreorderStatus.READY,
            gateway_merchant_id="6666000000000000",
            req_date="20260919",
            req_seq_id=order.order_no,
            payment_scene="official_account",
            trade_type="T_JSAPI",
            gateway_trade_no="HF-ACTIVITY-NOTIFY",
        )
        payload = json.dumps(
            {
                "huifu_id": payment.gateway_merchant_id,
                "req_date": payment.req_date,
                "req_seq_id": payment.req_seq_id,
                "hf_seq_id": payment.gateway_trade_no,
                "trans_stat": "S",
                "trans_amt": f"{order.payable_amount / 100:.2f}",
                "trans_type": "T_JSAPI",
                "notify_type": "1",
                "end_time": timezone.localtime().strftime("%Y%m%d%H%M%S"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        notification_gateway.return_value.verify_payment_notification.return_value = True
        activity_gateway.return_value.query_payment.return_value = HuifuPaymentQueryResult(
            req_date=payment.req_date,
            req_seq_id=payment.req_seq_id,
            huifu_id=payment.gateway_merchant_id,
            trans_stat="S",
            trans_amt=f"{order.payable_amount / 100:.2f}",
            end_time=timezone.localtime().strftime("%Y%m%d%H%M%S"),
            trade_type="T_JSAPI",
            gateway_trade_no=payment.gateway_trade_no,
            party_order_id="PARTY-ACTIVITY-NOTIFY",
            out_trans_id="OUT-ACTIVITY-NOTIFY",
            response_code="00000000",
            response_digest="e" * 64,
        )

        first = self.client.post(
            "/api/v1/payments/huifu/notify/",
            {"resp_data": payload, "sign": "verified-signature"},
        )
        second = self.client.post(
            "/api/v1/payments/huifu/notify/",
            {"resp_data": payload, "sign": "verified-signature"},
        )

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        activity_gateway.return_value.query_payment.assert_called_once()
        order.refresh_from_db()
        participation.refresh_from_db()
        self.assertEqual(order.status, ActivityParticipationPaymentOrder.Status.PAID)
        self.assertEqual(participation.status, ActivityParticipation.Status.ACTIVE)
        event = ActivityHuifuNotification.objects.get()
        self.assertTrue(event.signature_verified)
        self.assertEqual(event.status, ActivityHuifuNotification.Status.PROCESSED)

    @override_settings(DEBUG=True)
    @patch("activities.huifu.get_huifu_payment_gateway")
    def test_activity_real_refund_submits_then_confirms_by_query(self, gateway):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000103",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        participation, order, _created = get_or_create_participation_order(
            activity_id=activity.pk,
            user=participant,
            channel=ActivityParticipationPaymentOrder.Channel.WECHAT,
        )
        participation.status = ActivityParticipation.Status.ACTIVE
        participation.joined_at = timezone.now()
        participation.save(update_fields=("status", "joined_at", "updated_at"))
        order.status = ActivityParticipationPaymentOrder.Status.PAID
        order.paid_at = timezone.now()
        order.gateway_trade_no = "HF-ACTIVITY-ORIGINAL"
        order.save(update_fields=("status", "paid_at", "gateway_trade_no", "updated_at"))
        payment = ActivityHuifuPaymentOrder.objects.create(
            participation_order=order,
            preorder_status=ActivityHuifuPaymentOrder.PreorderStatus.READY,
            gateway_merchant_id="6666000000000000",
            req_date="20260919",
            req_seq_id=order.order_no,
            payment_scene="official_account",
            trade_type="T_JSAPI",
            gateway_trade_no=order.gateway_trade_no,
        )
        refund, _ = create_activity_participation_refund(
            participation=participation,
            payment_order=order,
            refund_type=ActivityParticipationRefundOrder.RefundType.PAYMENT_TIMEOUT,
            idempotency_key=f"real-refund:{order.order_no}",
            principal_refund_amount=order.aa_principal_amount,
            service_fee_refund_amount=order.platform_service_fee_amount,
            reason="测试真实原路退款",
        )
        gateway.return_value.merchant_id = payment.gateway_merchant_id
        gateway.return_value.refund_payment.return_value = HuifuRefundResult(
            req_date=timezone.localdate().strftime("%Y%m%d"),
            req_seq_id=refund.refund_no,
            huifu_id=payment.gateway_merchant_id,
            trans_stat="P",
            ord_amt=f"{refund.refund_amount / 100:.2f}",
            actual_ref_amt="",
            gateway_refund_no="HF-ACTIVITY-REFUND",
            trans_finish_time="",
            response_code="00000000",
            response_digest="c" * 64,
        )
        gateway.return_value.query_refund.return_value = HuifuRefundQueryResult(
            req_date=timezone.localdate().strftime("%Y%m%d"),
            req_seq_id=refund.refund_no,
            huifu_id=payment.gateway_merchant_id,
            trans_stat="S",
            ord_amt=f"{refund.refund_amount / 100:.2f}",
            actual_ref_amt=f"{refund.refund_amount / 100:.2f}",
            gateway_refund_no="HF-ACTIVITY-REFUND",
            trans_finish_time=timezone.localtime().strftime("%Y%m%d%H%M%S"),
            response_code="00000000",
            response_digest="d" * 64,
        )

        from .services import process_activity_participation_refund

        completed, changed = process_activity_participation_refund(refund.refund_no)

        self.assertTrue(changed)
        self.assertEqual(completed.status, ActivityParticipationRefundOrder.Status.SUCCEEDED)
        order.refresh_from_db()
        self.assertEqual(order.status, ActivityParticipationPaymentOrder.Status.REFUNDED)
        self.assertTrue(
            ActivityHuifuRefundOrder.objects.filter(
                participation_refund=refund,
                gateway_refund_no="HF-ACTIVITY-REFUND",
            ).exists()
        )
        confirmation = confirm_activity_huifu_payment_status(
            payment_kind="activity_participation",
            activity_id=activity.pk,
            user_id=participant.pk,
        )
        self.assertEqual(confirmation["state"], "refunded")

    @override_settings(
        DEBUG=True,
        WECHAT_OFFICIAL_ACCOUNT_APP_ID="wx-official-app-id",
        WECHAT_OFFICIAL_ACCOUNT_APP_SECRET="official-app-secret",
        WECHAT_OFFICIAL_ACCOUNT_OAUTH_CALLBACK_URL=(
            "https://api.example.test/api/v1/payments/wechat/oauth/callback/"
        ),
        WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL=(
            "https://m.example.test/#/pages/booking/payment"
        ),
    )
    @patch("activities.views.ensure_activity_real_payment_available")
    def test_activity_publish_wechat_authorization_returns_to_activity_cashier(
        self, _ensure_real_payment
    ):
        activity = self.build_activity(status=Activity.Status.DRAFT)
        activity.save()
        order = ActivityPublishOrder.objects.create(
            activity=activity,
            order_no="ACTIVITY-OAUTH-001",
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={},
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        self.client.force_login(self.organizer)

        authorization = self.client.get(
            f"/api/v1/activities/{activity.pk}/publish-order/payment-authorization/"
        )
        authorize_query = parse_qs(
            urlsplit(authorization.json()["data"]["authorize_url"]).query
        )
        self.client.logout()
        with patch(
            "orders.wechat_oauth._exchange_code",
            return_value=("organizer-openid", "organizer-unionid"),
        ):
            callback = self.client.get(
                "/api/v1/payments/wechat/oauth/callback/",
                {"code": "wechat-code", "state": authorize_query["state"][0]},
            )

        self.assertEqual(authorization.status_code, 200)
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(
            callback["Location"],
            (
                "https://m.example.test/#/pages/activities/publish-payment"
                f"?id={activity.pk}&wechatAuthorized=1"
            ),
        )
        order.refresh_from_db()
        self.assertEqual(order.status, ActivityPublishOrder.Status.PENDING_PAYMENT)

    def test_minimum_participants_cannot_exceed_capacity(self):
        activity = self.build_activity(capacity=4, min_participants=5)

        with self.assertRaises(ValidationError):
            activity.full_clean()

    def test_activity_list_filters_public_upcoming_activities(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.full_clean()
        activity.save()

        response = self.client.get(
            "/api/v1/activities/",
            {
                "longitude": "116.4039810",
                "latitude": "39.9150010",
                "ordering": "distance",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
        item = response.json()["data"]["items"][0]
        self.assertEqual(item["title"], "周末台球局")
        self.assertNotIn("meeting_point", item)
        self.assertNotIn("meeting_address", item)

    def test_activity_publish_rules_are_public_and_use_current_operation_settings(self):
        PlatformOperationSetting.objects.create(
            activity_minimum_advance_hours=24,
            activity_maximum_advance_days=45,
        )

        response = self.client.get("/api/v1/activity-publish-rules/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["minimum_advance_hours"], 24)
        self.assertEqual(data["maximum_advance_days"], 45)
        self.assertEqual(Decimal(str(data["service_fee_rate"])), Decimal("0.1000"))
        self.assertEqual(data["min_capacity"], 2)
        self.assertEqual(data["max_capacity"], 100)

    def test_activity_detail_returns_display_fields_and_calculated_fee(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            aa_principal_amount=1045,
        )
        activity.full_clean()
        activity.save()

        response = self.client.get(f"/api/v1/activities/{activity.pk}/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["meeting_address"], "北京市测试地址")
        self.assertEqual(data["participant_count"], 0)
        self.assertEqual(data["platform_service_fee_amount"], 105)
        self.assertEqual(data["payable_amount"], 1150)

    def test_activity_detail_returns_not_found_for_unknown_id(self):
        response = self.client.get("/api/v1/activities/999999/")

        self.assertEqual(response.status_code, 404)

    def test_activity_draft_detail_is_only_visible_to_organizer(self):
        activity = self.build_activity(status=Activity.Status.DRAFT)
        activity.save()

        anonymous = self.client.get(f"/api/v1/activities/{activity.pk}/")
        self.client.force_login(self.organizer)
        organizer = self.client.get(f"/api/v1/activities/{activity.pk}/")

        self.assertEqual(anonymous.status_code, 404)
        self.assertEqual(organizer.status_code, 200)

    @override_settings(DEBUG=True)
    def test_participation_join_is_idempotent_and_visible_in_detail(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000009", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)

        first = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/",
            {"channel": "mock_wechat"},
            content_type="application/json",
        )
        second = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        paid = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        detail = self.client.get(f"/api/v1/activities/{activity.pk}/")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(paid.status_code, 200, paid.json())
        self.assertEqual(ActivityParticipation.objects.count(), 1)
        self.assertEqual(detail.json()["data"]["participant_count"], 1)
        self.assertTrue(detail.json()["data"]["is_joined"])
        self.assertEqual(detail.json()["data"]["participation_status"], "active")
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=participant,
                event_type=UserNotification.EventType.ACTIVITY_SIGNUP_SUCCESS,
                target_id=str(activity.pk),
            ).exists()
        )
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.organizer,
                event_type=UserNotification.EventType.ACTIVITY_SIGNUP_SUCCESS,
                target_id=str(activity.pk),
            ).exists()
        )

    @override_settings(DEBUG=True)
    def test_participation_forms_activity_and_cancel_reopens_it(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            min_participants=2,
            capacity=3,
        )
        activity.save()
        first = User.objects.create_user(
            phone="13800000010", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        second = User.objects.create_user(
            phone="13800000011", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )

        self.client.force_login(first)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        self.client.force_login(second)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        joined = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        activity.refresh_from_db()

        self.assertEqual(joined.status_code, 200, joined.json())
        self.assertEqual(activity.status, Activity.Status.FORMED)
        self.assertEqual(joined.json()["data"]["participant_count"], 2)
        self.assertEqual(
            UserNotification.objects.filter(
                event_type=UserNotification.EventType.ACTIVITY_FORMED,
                target_id=str(activity.pk),
            ).count(),
            3,
        )

        cancelled = self.client.delete(f"/api/v1/activities/{activity.pk}/participation/")
        activity.refresh_from_db()

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(activity.status, Activity.Status.RECRUITING)
        self.assertEqual(
            ActivityParticipation.objects.filter(status="active").count(),
            1,
        )

        third = User.objects.create_user(
            phone="13800000012", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(third)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        reformed = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        activity.refresh_from_db()

        self.assertEqual(reformed.status_code, 200, reformed.json())
        self.assertEqual(activity.status, Activity.Status.FORMED)
        self.assertEqual(
            UserNotification.objects.filter(
                event_type=UserNotification.EventType.ACTIVITY_FORMED,
                target_id=str(activity.pk),
            ).count(),
            6,
        )

    @override_settings(DEBUG=True)
    def test_participation_rejects_organizer_and_full_activity(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            min_participants=2,
            capacity=2,
        )
        activity.save()
        self.client.force_login(self.organizer)
        organizer_response = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/"
        )
        self.assertEqual(organizer_response.status_code, 403)

        for suffix in (12, 13):
            user = User.objects.create_user(
                phone=f"138000000{suffix}", password="test",
                verification_status=User.VerificationStatus.VERIFIED,
            )
            self.client.force_login(user)
            self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
            self.client.post(
                f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
            )

        extra = User.objects.create_user(
            phone="13800000014", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(extra)
        full_response = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/"
        )

        self.assertEqual(full_response.status_code, 400)
        self.assertIn("名额已满", str(full_response.json()))

    def test_my_activities_lists_joined_and_cancelled_records(self):
        active_activity = self.build_activity(
            title="正在参与的活动", status=Activity.Status.RECRUITING
        )
        active_activity.save()
        cancelled_activity = self.build_activity(
            title="已经取消的活动", status=Activity.Status.RECRUITING
        )
        cancelled_activity.save()
        participant = User.objects.create_user(phone="13800000015", password="test")
        ActivityParticipation.objects.create(
            activity=active_activity,
            user=participant,
            status=ActivityParticipation.Status.ACTIVE,
            joined_at=timezone.now(),
        )
        ActivityParticipation.objects.create(
            activity=cancelled_activity,
            user=participant,
            status=ActivityParticipation.Status.CANCELLED,
            cancelled_at=timezone.now(),
        )
        self.client.force_login(participant)

        upcoming = self.client.get("/api/v1/activities/mine/", {"state": "upcoming"})
        history = self.client.get("/api/v1/activities/mine/", {"state": "history"})

        self.assertEqual(upcoming.status_code, 200)
        self.assertEqual(upcoming.json()["data"]["pagination"]["total"], 1)
        self.assertEqual(
            upcoming.json()["data"]["items"][0]["participation_status"], "active"
        )
        self.assertEqual(history.json()["data"]["pagination"]["total"], 1)
        self.assertEqual(
            history.json()["data"]["items"][0]["participation_status"], "cancelled"
        )

    def test_my_activities_lists_organized_activities(self):
        activity = self.build_activity(
            title="我发起的活动", status=Activity.Status.RECRUITING
        )
        activity.save()
        self.client.force_login(self.organizer)

        response = self.client.get(
            "/api/v1/activities/mine/", {"role": "organized"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
        item = response.json()["data"]["items"][0]
        self.assertEqual(item["title"], "我发起的活动")
        self.assertIsNone(item["participation_status"])

    @override_settings(DEBUG=True)
    def test_participation_payment_locks_seat_and_timeout_releases_it(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING, capacity=2, min_participants=2
        )
        activity.save()
        first = User.objects.create_user(
            phone="13800000031", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        second = User.objects.create_user(
            phone="13800000032", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        third = User.objects.create_user(
            phone="13800000033", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        for user in (first, second):
            self.client.force_login(user)
            response = self.client.post(
                f"/api/v1/activities/{activity.pk}/participation/"
            )
            self.assertEqual(response.status_code, 201)
        self.client.force_login(third)
        full = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.assertEqual(full.status_code, 400)

        first_participation = ActivityParticipation.objects.get(
            activity=activity, user=first
        )
        first_participation.payment_orders.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        first_participation.payment_expires_at = timezone.now() - timedelta(seconds=1)
        first_participation.save(update_fields=("payment_expires_at",))
        retried = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")

        self.assertEqual(retried.status_code, 201)
        first_participation.refresh_from_db()
        self.assertEqual(first_participation.status, ActivityParticipation.Status.EXPIRED)
        self.assertEqual(
            first_participation.payment_orders.get().status,
            ActivityParticipationPaymentOrder.Status.CLOSED,
        )

    @override_settings(DEBUG=True)
    def test_participation_uses_configured_payment_timeout(self):
        PlatformOperationSetting.objects.create(activity_payment_timeout_minutes=10)
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000039",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        created_after = timezone.now()

        response = self.client.post(f"/api/v1/activities/{activity.pk}/participation/")

        self.assertEqual(response.status_code, 201)
        payment = ActivityParticipationPaymentOrder.objects.get(
            participation__activity=activity,
            payer=participant,
        )
        self.assertGreaterEqual(
            payment.expires_at,
            created_after + timedelta(minutes=10),
        )
        self.assertLess(
            payment.expires_at,
            created_after + timedelta(minutes=10, seconds=2),
        )

    @override_settings(DEBUG=True)
    def test_participation_payment_rejects_gateway_amount_mismatch(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000040",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")

        with patch(
            "activities.payment_gateway.MockActivityPaymentGateway.confirm_payment",
            return_value=PaymentResult(
                gateway_trade_no="MISMATCHED-ACTIVITY-AMOUNT",
                paid_amount=1,
                signature_verified=True,
            ),
        ):
            response = self.client.post(
                f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
            )

        self.assertEqual(response.status_code, 400)
        participation = ActivityParticipation.objects.get(
            activity=activity,
            user=participant,
        )
        self.assertEqual(
            participation.status,
            ActivityParticipation.Status.PENDING_PAYMENT,
        )
        self.assertEqual(
            participation.payment_orders.get().status,
            ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT,
        )

    @override_settings(DEBUG=True)
    def test_participant_cancellation_creates_rule_based_refund_idempotently(self):
        starts_at = timezone.now() + timedelta(hours=4)
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            formation_deadline=timezone.now() + timedelta(hours=1),
        )
        activity.save()
        participant = User.objects.create_user(
            phone="13800000034", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )

        first = self.client.delete(
            f"/api/v1/activities/{activity.pk}/participation/"
        )
        second = self.client.delete(
            f"/api/v1/activities/{activity.pk}/participation/"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ActivityParticipationRefundOrder.objects.count(), 1)
        refund = ActivityParticipationRefundOrder.objects.get()
        self.assertEqual(refund.status, ActivityParticipationRefundOrder.Status.PENDING)
        with self.captureOnCommitCallbacks(execute=True):
            processed = process_due_tasks(
                task_types=[ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND]
            )
        self.assertEqual(processed["succeeded"], 1)
        refund.refresh_from_db()
        self.assertEqual(refund.status, ActivityParticipationRefundOrder.Status.SUCCEEDED)
        self.assertEqual(refund.principal_refund_amount, 3360)
        self.assertEqual(refund.service_fee_refund_amount, 0)
        self.assertEqual(refund.retained_principal_amount, 1440)
        self.assertEqual(
            refund.retained_principal_destination,
            ActivityParticipationRefundOrder.PrincipalDestination.ORGANIZER,
        )
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=participant,
                event_type=UserNotification.EventType.ACTIVITY_REFUND_COMPLETED,
                target_id=str(activity.pk),
            ).count(),
            1,
        )

    @override_settings(DEBUG=True)
    def test_zero_amount_cancellation_is_completed_locally_without_gateway_task(self):
        starts_at = timezone.now() + timedelta(hours=1)
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            formation_deadline=timezone.now() + timedelta(minutes=20),
        )
        activity.save()
        participant = User.objects.create_user(
            phone="13800000041",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )

        with patch(
            "activities.payment_gateway.MockActivityPaymentGateway.refund"
        ) as gateway_refund:
            response = self.client.delete(
                f"/api/v1/activities/{activity.pk}/participation/"
            )
            processed = process_due_tasks(
                task_types=[ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND]
            )

        self.assertEqual(response.status_code, 200)
        refund = ActivityParticipationRefundOrder.objects.get()
        self.assertEqual(refund.refund_amount, 0)
        self.assertEqual(
            refund.status, ActivityParticipationRefundOrder.Status.SUCCEEDED
        )
        self.assertIsNotNone(refund.refunded_at)
        self.assertEqual(refund.retained_principal_amount, 4800)
        self.assertEqual(refund.retained_service_fee_amount, 480)
        self.assertEqual(
            refund.retained_principal_destination,
            ActivityParticipationRefundOrder.PrincipalDestination.ORGANIZER,
        )
        self.assertEqual(processed["claimed"], 0)
        self.assertFalse(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
                business_key=refund.refund_no,
            ).exists()
        )
        gateway_refund.assert_not_called()

    def test_failed_refund_keeps_paid_amount_reserved(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant_user = User.objects.create_user(
            phone="13800000042", password="test"
        )
        participation = ActivityParticipation.objects.create(
            activity=activity,
            user=participant_user,
            status=ActivityParticipation.Status.CANCELLED,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
        )
        payment = ActivityParticipationPaymentOrder.objects.create(
            participation=participation,
            payer=participant_user,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityParticipationPaymentOrder.Status.PAID,
            expires_at=timezone.now() + timedelta(minutes=30),
            paid_at=timezone.now(),
        )
        refund, _ = create_activity_participation_refund(
            participation=participation,
            payment_order=payment,
            refund_type=ActivityParticipationRefundOrder.RefundType.AFTER_SALES,
            idempotency_key="failed-refund-reservation-a",
            principal_refund_amount=4800,
            service_fee_refund_amount=480,
            reason="首次退款进入失败状态",
        )
        refund.status = ActivityParticipationRefundOrder.Status.FAILED
        refund.failure_reason = "模拟渠道失败"
        refund.save(update_fields=("status", "failure_reason", "updated_at"))

        with self.assertRaises(DRFValidationError):
            create_activity_participation_refund(
                participation=participation,
                payment_order=payment,
                refund_type=ActivityParticipationRefundOrder.RefundType.AFTER_SALES,
                idempotency_key="failed-refund-reservation-b",
                principal_refund_amount=4800,
                service_fee_refund_amount=480,
                reason="不应创建的第二张全额退款",
            )

        self.assertEqual(ActivityParticipationRefundOrder.objects.count(), 1)

    @override_settings(DEBUG=True)
    def test_activity_after_sales_is_idempotent_and_links_paid_participation(self):
        activity = self.build_activity(status=Activity.Status.RECRUITING)
        activity.save()
        participant = User.objects.create_user(
            phone="13800000035", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        payload = {
            "reason": "illness_or_accident",
            "description": "突发身体不适，申请平台协助退款。",
        }

        first = self.client.post(
            f"/api/v1/activities/{activity.pk}/after-sales/",
            payload,
            content_type="application/json",
        )
        second = self.client.post(
            f"/api/v1/activities/{activity.pk}/after-sales/",
            payload,
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["case_no"], second.json()["data"]["case_no"])
        case = ActivityAfterSalesCase.objects.get()
        self.assertEqual(case.requested_amount, 5280)
        self.assertEqual(case.status, ActivityAfterSalesCase.Status.PENDING)
        cancellation = self.client.delete(
            f"/api/v1/activities/{activity.pk}/participation/"
        )
        self.assertEqual(cancellation.status_code, 400)
        self.assertEqual(ActivityParticipationRefundOrder.objects.count(), 0)

    def test_timeout_processor_fails_unformed_activity_and_refunds_publish_order(self):
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            formation_deadline=timezone.now() - timedelta(minutes=1),
        )
        activity.save()
        ActivityPublishOrder.objects.create(
            order_no="FAILED-TO-FORM-PUBLISH",
            activity=activity,
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityPublishOrder.Status.PAID,
            expires_at=timezone.now() - timedelta(days=1),
            paid_at=timezone.now() - timedelta(days=1),
        )

        result = process_activity_timeouts()

        activity.refresh_from_db()
        self.assertEqual(result["processed_activity_count"], 1)
        self.assertEqual(activity.status, Activity.Status.FAILED_TO_FORM)
        self.assertEqual(activity.publish_orders.get().status, ActivityPublishOrder.Status.REFUNDED)
        self.assertEqual(
            activity.refund_records.get().refund_type,
            "failed_to_form",
        )
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.organizer,
                event_type=UserNotification.EventType.ACTIVITY_FAILED_TO_FORM,
                target_id=str(activity.pk),
            ).exists()
        )

    def test_activity_lifecycle_creates_and_advances_settlement_idempotently(self):
        PlatformOperationSetting.objects.create(
            activity_settlement_confirmation_hours=48,
            activity_settlement_risk_freeze_days=3,
        )
        now = timezone.now()
        activity = self.build_activity(
            status=Activity.Status.FORMED,
            starts_at=now - timedelta(hours=3),
            ends_at=now - timedelta(hours=1),
            formation_deadline=now - timedelta(hours=4),
            min_participants=2,
        )
        activity.save()
        ActivityPublishOrder.objects.create(
            order_no="SETTLEMENT-PUBLISH",
            activity=activity,
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityPublishOrder.Status.PAID,
            expires_at=now - timedelta(days=1),
            paid_at=now - timedelta(days=1),
        )
        participant = User.objects.create_user(phone="13800000038", password="test")
        participation = ActivityParticipation.objects.create(
            activity=activity,
            user=participant,
            status=ActivityParticipation.Status.ACTIVE,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            joined_at=now - timedelta(days=1),
        )
        ActivityParticipationPaymentOrder.objects.create(
            participation=participation,
            payer=participant,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityParticipationPaymentOrder.Status.PAID,
            expires_at=now - timedelta(hours=20),
            paid_at=now - timedelta(hours=21),
        )

        first = process_activity_timeouts(now=now)
        activity.refresh_from_db()
        settlement = activity.settlement
        self.assertEqual(
            settlement.confirmation_deadline,
            activity.ends_at + timedelta(hours=48),
        )
        self.assertEqual(
            settlement.freeze_until,
            settlement.confirmation_deadline + timedelta(days=3),
        )

        self.assertEqual(first["started_activity_count"], 1)
        self.assertEqual(first["completed_activity_count"], 1)
        self.assertEqual(first["settlement_created_count"], 1)
        self.assertEqual(activity.status, Activity.Status.COMPLETED)
        self.assertEqual(settlement.status, ActivitySettlement.Status.CONFIRMING)
        self.assertEqual(settlement.settlement_amount, 9600)
        self.assertEqual(settlement.platform_service_fee_amount, 960)

        process_activity_timeouts(now=settlement.confirmation_deadline)
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, ActivitySettlement.Status.RISK_FROZEN)

        process_activity_timeouts(now=settlement.freeze_until)
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, ActivitySettlement.Status.SETTLED)
        self.assertEqual(ActivitySettlement.objects.filter(activity=activity).count(), 1)
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.organizer,
                event_type=UserNotification.EventType.ACTIVITY_SETTLED,
                target_id=str(activity.pk),
            ).count(),
            1,
        )

        self.client.force_login(self.organizer)
        organizer_detail = self.client.get(f"/api/v1/activities/{activity.pk}/")
        self.client.force_login(participant)
        participant_detail = self.client.get(f"/api/v1/activities/{activity.pk}/")
        self.assertEqual(
            organizer_detail.json()["data"]["settlement"]["settlement_amount"], 9600
        )
        self.assertIsNone(
            participant_detail.json()["data"]["settlement"]["settlement_amount"]
        )

    @override_settings(DEBUG=True)
    def test_organizer_cancellation_refunds_participants_and_applies_responsibility(self):
        starts_at = timezone.now() + timedelta(hours=4)
        activity = self.build_activity(
            status=Activity.Status.RECRUITING,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            formation_deadline=timezone.now() + timedelta(hours=1),
            published_at=timezone.now() - timedelta(days=1),
        )
        activity.save()
        ActivityPublishOrder.objects.create(
            order_no="ORGANIZER-CANCEL-ORDER",
            activity=activity,
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityPublishOrder.Status.PAID,
            expires_at=timezone.now() - timedelta(days=1),
            paid_at=timezone.now() - timedelta(days=1),
        )
        participant = User.objects.create_user(
            phone="13800000036", password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        self.client.post(f"/api/v1/activities/{activity.pk}/participation/")
        self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/simulate-payment/"
        )
        self.client.force_login(self.organizer)

        response = self.client.post(
            f"/api/v1/activities/{activity.pk}/cancel/",
            {"reason": "场地临时无法使用"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        activity.refresh_from_db()
        self.assertEqual(activity.status, Activity.Status.CANCELLED)
        participant_refund = ActivityParticipationRefundOrder.objects.get()
        self.assertEqual(participant_refund.refund_amount, 5280)
        publish_refund = activity.refund_records.get()
        self.assertEqual(publish_refund.principal_amount, 3360)
        self.assertEqual(publish_refund.service_fee_amount, 0)
        self.assertEqual(publish_refund.retained_principal_destination, "platform")

    def activity_create_payload(self, **overrides):
        starts_at = timezone.now() + timedelta(days=3)
        payload = {
            "category_slug": self.category.slug,
            "title": "我发布的桌游活动",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=3)).isoformat(),
            "formation_deadline": (starts_at - timedelta(hours=12)).isoformat(),
            "meeting_place_name": "美乐城桌游空间",
            "meeting_address": "邯郸市丛台区人民路",
            "longitude": "114.5389610",
            "latitude": "36.6256570",
            "capacity": 8,
            "min_participants": 4,
            "description": "轻松认识新朋友",
            "participation_rules": "准时到场，文明参与",
            "aa_principal_amount": 6800,
            "refund_template_version": "standard-v1",
            "cover_id": str(self.cover.pk),
        }
        payload.update(overrides)
        return payload

    @override_settings(DEBUG=True)
    def test_verified_user_can_create_paid_activity_draft(self):
        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        self.client.force_login(self.organizer)

        response = self.client.post(
            "/api/v1/activities/", self.activity_create_payload(), content_type="application/json"
        )

        self.assertEqual(response.status_code, 201)
        activity = Activity.objects.get(title="我发布的桌游活动")
        self.assertEqual(activity.status, Activity.Status.DRAFT)
        self.assertEqual(activity.refund_template_version, "standard-v1")
        self.assertEqual(response.json()["data"]["next_step"], "payment")

        payment_order = self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/"
        )
        first_order_no = payment_order.json()["data"]["order_no"]
        same_order = self.client.post(f"/api/v1/activities/{activity.pk}/publish-order/")
        self.assertEqual(same_order.json()["data"]["order_no"], first_order_no)
        ActivityPublishOrder.objects.filter(order_no=first_order_no).update(
            status=ActivityPublishOrder.Status.CANCELLED
        )
        retry_order = self.client.post(f"/api/v1/activities/{activity.pk}/publish-order/")
        paid = self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/simulate-payment/"
        )
        paid_again = self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/simulate-payment/"
        )
        recovered = self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/"
        )
        activity.refresh_from_db()
        order = ActivityPublishOrder.objects.get(
            activity=activity, status=ActivityPublishOrder.Status.PAID
        )

        self.assertEqual(payment_order.status_code, 201)
        self.assertEqual(payment_order.json()["data"]["payable_amount"], 7480)
        self.assertEqual(retry_order.status_code, 201)
        self.assertNotEqual(retry_order.json()["data"]["order_no"], first_order_no)
        self.assertEqual(ActivityPublishOrder.objects.filter(activity=activity).count(), 2)
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(paid_again.status_code, 200)
        self.assertEqual(recovered.status_code, 201)
        self.assertEqual(recovered.json()["data"]["status"], "paid")
        self.assertEqual(
            paid_again.json()["data"]["order_no"], retry_order.json()["data"]["order_no"]
        )
        self.assertEqual(order.status, ActivityPublishOrder.Status.PAID)
        self.assertEqual(activity.status, Activity.Status.PENDING_REVIEW)
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.organizer,
                event_type=UserNotification.EventType.ACTIVITY_PUBLISH_SUBMITTED,
                target_id=str(activity.pk),
            ).exists()
        )

    @override_settings(DEBUG=True)
    def test_tags_default_cover_and_fee_rate_are_snapshotted_for_both_payment_flows(self):
        second_tag = ActivityCategory.objects.create(name="交友", slug="social")
        setting = PlatformOperationSetting.current()
        setting.activity_service_fee_rate = Decimal("0.1250")
        setting.default_activity_cover = self.cover
        setting.save(
            update_fields=("activity_service_fee_rate", "default_activity_cover", "updated_at")
        )
        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        self.client.force_login(self.organizer)
        payload = self.activity_create_payload(
            tag_slugs=[self.category.slug, second_tag.slug]
        )
        payload.pop("category_slug")
        payload.pop("cover_id")

        created = self.client.post(
            "/api/v1/activities/", payload, content_type="application/json"
        )

        self.assertEqual(created.status_code, 201)
        activity = Activity.objects.get(pk=created.json()["data"]["id"])
        self.assertEqual(activity.cover_id, self.cover.pk)
        self.assertEqual(activity.service_fee_rate, Decimal("0.1250"))
        self.assertEqual(
            list(activity.tags.order_by("id").values_list("slug", flat=True)),
            [self.category.slug, second_tag.slug],
        )

        setting.activity_service_fee_rate = Decimal("0.2000")
        setting.save(update_fields=("activity_service_fee_rate", "updated_at"))
        publish_order = self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/"
        ).json()["data"]
        self.assertEqual(publish_order["platform_service_fee_amount"], 850)
        self.assertEqual(publish_order["payable_amount"], 7650)
        self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/simulate-payment/"
        )
        activity.status = Activity.Status.RECRUITING
        activity.save(update_fields=("status", "updated_at"))

        participant = User.objects.create_user(
            phone="13800000139",
            password="test",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.client.force_login(participant)
        participation = self.client.post(
            f"/api/v1/activities/{activity.pk}/participation/",
            {},
            content_type="application/json",
        )
        self.assertEqual(participation.status_code, 201)
        payment_order = participation.json()["data"]["payment_order"]
        self.assertEqual(payment_order["platform_service_fee_amount"], 850)
        self.assertEqual(payment_order["payable_amount"], 7650)

        notification = UserNotification.objects.get(
            recipient=self.organizer,
            event_type=UserNotification.EventType.ACTIVITY_PUBLISH_SUBMITTED,
        )
        self.assertEqual(notification.action_text, "查看进展")
        self.assertEqual(notification.action_url, "/pages/activities/mine?role=organized")

    @override_settings(DEBUG=True)
    def test_legacy_category_publish_cannot_bypass_platform_limits(self):
        setting = PlatformOperationSetting.current()
        setting.activity_max_capacity = 10
        setting.activity_max_aa_principal_amount = 10_000
        setting.save(
            update_fields=(
                "activity_max_capacity",
                "activity_max_aa_principal_amount",
                "updated_at",
            )
        )
        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        self.client.force_login(self.organizer)

        capacity_response = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(capacity=11),
            content_type="application/json",
        )
        amount_response = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(aa_principal_amount=10_001),
            content_type="application/json",
        )

        self.assertEqual(capacity_response.status_code, 400)
        self.assertIn("capacity", capacity_response.json())
        self.assertEqual(amount_response.status_code, 400)
        self.assertIn("aa_principal_amount", amount_response.json())

    @override_settings(DEBUG=True)
    def test_tag_payload_uses_first_tag_as_legacy_primary_tag(self):
        second_tag = ActivityCategory.objects.create(name="交友", slug="social-primary")
        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        self.client.force_login(self.organizer)
        payload = self.activity_create_payload(
            category_slug=second_tag.slug,
            tag_slugs=[self.category.slug, second_tag.slug],
        )

        response = self.client.post(
            "/api/v1/activities/", payload, content_type="application/json"
        )

        self.assertEqual(response.status_code, 201)
        activity = Activity.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(activity.category_id, self.category.pk)

    def test_activity_list_supports_city_keyword_and_multi_tag_filters(self):
        social = ActivityCategory.objects.create(name="交友", slug="social-filter")
        target = self.build_activity(
            title="城市桌游交友夜",
            city_code="130400",
            city_name="邯郸市",
            status=Activity.Status.RECRUITING,
        )
        target.save()
        target.tags.set([self.category, social])
        other = self.build_activity(
            title="北京周末活动",
            city_code="110100",
            city_name="北京市",
            status=Activity.Status.RECRUITING,
        )
        other.save()
        other.tags.set([self.category])

        response = self.client.get(
            "/api/v1/activities/",
            {"city_code": "130400", "keyword": "交友", "tags": "social-filter"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
        item = response.json()["data"]["items"][0]
        self.assertEqual(item["id"], target.pk)
        self.assertEqual([tag["slug"] for tag in item["tags"]], ["billiards", "social-filter"])

    @override_settings(DEBUG=True)
    def test_activity_publish_payment_expires_through_task_center(self):
        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        self.client.force_login(self.organizer)
        created = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(),
            content_type="application/json",
        )
        activity = Activity.objects.get(pk=created.json()["data"]["id"])
        response = self.client.post(
            f"/api/v1/activities/{activity.pk}/publish-order/"
        )
        order = ActivityPublishOrder.objects.get(
            order_no=response.json()["data"]["order_no"]
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            parse_datetime(response.json()["data"]["expires_at"]),
            order.expires_at,
        )
        processed = process_due_tasks(
            task_types=[ScheduledTask.Type.ACTIVITY_PUBLISH_PAYMENT_EXPIRY],
            now=order.expires_at,
        )

        self.assertEqual(processed["succeeded"], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, ActivityPublishOrder.Status.CANCELLED)
        self.assertIsNotNone(order.closed_at)
        activity.refresh_from_db()
        self.assertEqual(activity.status, Activity.Status.DRAFT)

    @override_settings(DEBUG=True)
    def test_activity_create_does_not_require_identity_and_enforces_start_window(self):
        self.client.force_login(self.organizer)
        unverified = self.client.post(
            "/api/v1/activities/", self.activity_create_payload(), content_type="application/json"
        )
        self.assertEqual(unverified.status_code, 201)
        too_soon = timezone.now() + timedelta(hours=24)
        invalid = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(
                starts_at=too_soon.isoformat(),
                ends_at=(too_soon + timedelta(hours=2)).isoformat(),
                formation_deadline=(too_soon - timedelta(hours=2)).isoformat(),
            ),
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("48小时", str(invalid.json()))

    @override_settings(DEBUG=False)
    def test_activity_draft_is_not_created_without_an_available_payment_path(self):
        self.client.force_login(self.organizer)

        response = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("活动发布真实支付尚未开放", str(response.json()))
        self.assertFalse(Activity.objects.filter(title="我发布的桌游活动").exists())

    def test_activity_create_honors_category_city_people_and_amount_rules(self):
        self.category.city_codes = ["130400"]
        self.category.min_capacity = 4
        self.category.max_capacity = 8
        self.category.min_aa_principal_amount = 5000
        self.category.max_aa_principal_amount = 8000
        self.category.save()
        self.organizer.verification_status = User.VerificationStatus.VERIFIED
        self.organizer.save(update_fields=("verification_status",))
        self.client.force_login(self.organizer)

        wrong_city = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(city_code="110100", city_name="北京市"),
            content_type="application/json",
        )
        too_many = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(
                city_code="130400", city_name="邯郸市", capacity=9
            ),
            content_type="application/json",
        )
        too_expensive = self.client.post(
            "/api/v1/activities/",
            self.activity_create_payload(
                city_code="130400", city_name="邯郸市", aa_principal_amount=8100
            ),
            content_type="application/json",
        )

        self.assertEqual(wrong_city.status_code, 400)
        self.assertEqual(too_many.status_code, 400)
        self.assertEqual(too_expensive.status_code, 400)

    def test_activity_report_is_idempotent_and_private_draft_is_not_reportable(self):
        public_activity = self.build_activity(status=Activity.Status.RECRUITING)
        public_activity.save()
        draft = self.build_activity(title="私有草稿", status=Activity.Status.DRAFT)
        draft.save()
        reporter = User.objects.create_user(phone="13800000088", password="test")
        self.client.force_login(reporter)

        first = self.client.post(
            f"/api/v1/activities/{public_activity.pk}/reports/",
            {"reason": "false_information", "description": "信息与现场不一致"},
            content_type="application/json",
        )
        second = self.client.post(
            f"/api/v1/activities/{public_activity.pk}/reports/",
            {"reason": "other"},
            content_type="application/json",
        )
        private = self.client.post(
            f"/api/v1/activities/{draft.pk}/reports/",
            {"reason": "other"},
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["case_no"], second.json()["data"]["case_no"])
        self.assertEqual(private.status_code, 404)


class ActivityRefundConcurrencyTests(TransactionTestCase):
    def setUp(self):
        organizer = User.objects.create_user(phone="13800000901", password="test")
        participant_user = User.objects.create_user(
            phone="13800000902", password="test"
        )
        category = ActivityCategory.objects.create(
            name="并发退款测试", slug="concurrent-refund"
        )
        starts_at = timezone.now() + timedelta(days=3)
        activity = Activity.objects.create(
            organizer=organizer,
            category=category,
            title="活动退款并发测试",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            formation_deadline=starts_at - timedelta(hours=12),
            meeting_place_name="并发测试场地",
            meeting_address="邯郸市测试地址",
            city_code="130400",
            city_name="邯郸市",
            source_longitude=Decimal("114.4921000"),
            source_latitude=Decimal("36.6123000"),
            meeting_point=Point(114.4859, 36.6118, srid=4326),
            capacity=8,
            min_participants=4,
            description="并发退款测试活动",
            participation_rules="测试规则",
            aa_principal_amount=4800,
            refund_template_version="standard-v1",
            refund_rule_snapshot={"version": "standard-v1"},
            status=Activity.Status.RECRUITING,
        )
        self.participation = ActivityParticipation.objects.create(
            activity=activity,
            user=participant_user,
            status=ActivityParticipation.Status.ACTIVE,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            joined_at=timezone.now(),
        )
        self.payment = ActivityParticipationPaymentOrder.objects.create(
            participation=self.participation,
            payer=participant_user,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityParticipationPaymentOrder.Status.PAID,
            expires_at=timezone.now() + timedelta(minutes=30),
            paid_at=timezone.now(),
        )

    def test_concurrent_full_refunds_reserve_paid_amount_once(self):
        barrier = Barrier(2)

        def create_refund(idempotency_key):
            close_old_connections()
            try:
                participation = ActivityParticipation.objects.get(
                    pk=self.participation.pk
                )
                payment = ActivityParticipationPaymentOrder.objects.get(
                    pk=self.payment.pk
                )
                barrier.wait(timeout=5)
                create_activity_participation_refund(
                    participation=participation,
                    payment_order=payment,
                    refund_type=(
                        ActivityParticipationRefundOrder.RefundType.AFTER_SALES
                    ),
                    idempotency_key=idempotency_key,
                    principal_refund_amount=4800,
                    service_fee_refund_amount=480,
                    reason="并发创建全额退款",
                )
                return "created"
            except DRFValidationError:
                return "rejected"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    create_refund,
                    ("concurrent-refund-a", "concurrent-refund-b"),
                )
            )

        self.assertCountEqual(outcomes, ("created", "rejected"))
        self.assertEqual(ActivityParticipationRefundOrder.objects.count(), 1)
        self.assertEqual(
            ActivityParticipationRefundOrder.objects.get().refund_amount, 5280
        )
