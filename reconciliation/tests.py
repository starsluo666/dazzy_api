"""Offline reconciliation regressions. Synthetic bills only; all network mocked."""
import csv
import io
import uuid
import zipfile
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from backoffice import tests as fixtures
from orders.distributions import _scope
from orders.huifu import HuifuPaymentConfig
from orders.test_distributions import SETTINGS, MERCHANT
from providers.test_huifu_user_transport import signed_http_response
from wallets.models import WalletRechargeOrder, WalletPaymentAllocation
from .comparison import HEADERS, normalize, compare, finding
from .gateway import BillError, BillPending, money, read_archives, fetch_bill, download_bill
from .local_records import records_for
from .models import ReconciliationBatch as Batch, ReconciliationIssue as Issue, ReconciliationIssueEvent as Event
from .services import run_batch, ensure_day, daily, save_findings

DAY = date(2026, 10, 18)
NOW = datetime(2026, 10, 20, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
OPTIONS = {**SETTINGS, "HUIFU_RECONCILIATION_ENABLED": True,
           "HUIFU_RECONCILIATION_START_DATE": "2026-10-18"}
TRADE_HEADER = sorted(HEADERS["TRADE_BILL"] | {"原汇付交易订单号"})


def trade(**changes):
    return {"商户号": MERCHANT, "汇付订单号": "HF-1", "请求流水号": "REQ-1", "请求日期": "20261018",
            "交易类型": "消费", "交易金额": "10.00", "交易状态": "成功", "商户手续费": "0.06",
            "原汇付交易订单号": "", **changes}


def archive(rows=None, header=None, *, encoding="utf-8-sig", name="bill.csv"):
    stream, data = io.StringIO(newline=""), io.BytesIO()
    writer = csv.DictWriter(stream, fieldnames=header or TRADE_HEADER)
    writer.writeheader()
    writer.writerows([trade()] if rows is None else rows)
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("bill.csv")
        info.filename = name  # Preserve malicious backslashes on Windows for the test.
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, stream.getvalue().encode(encoding))
    return data.getvalue()


def local(**changes):
    return {"key": ("20261018", "REQ-1"), "kind": "payment", "reference": "ORDER-1",
            "amount": 1000, "status": "成功", "trade": "HF-1", "fee": 6,
            "recipients": None, "original": "", "expected": True, **changes}


class ParserTests(SimpleTestCase):
    def test_official_named_columns_and_encodings_with_blank_column(self):
        for encoding in ("utf-8-sig", "gb18030"):
            with self.subTest(encoding=encoding):
                raw = read_archives([archive(encoding=encoding, header=[*TRADE_HEADER, ""])])
                rows = normalize(raw, "TRADE_BILL", MERCHANT)
                self.assertEqual(rows[0]["amount"], 1000)
                self.assertEqual(compare(rows, [local()], "TRADE_BILL")[0], [])

    def test_yuan_money_is_exact_and_rejects_invalid_values(self):
        for value, expected in (("10", 1000), ("0.04", 4), ("1.5", 150), (" 0 ", 0)):
            self.assertEqual(money(value), expected)
        for value in ("NaN", "-1", "1e2", "1.001", "", "1,000", "01.0"):
            with self.subTest(value=value), self.assertRaises(BillError):
                money(value)

    def test_unsafe_archives_rejected_without_extraction(self):
        for name in ("../bill.csv", "/bill.csv", "folder\\bill.csv", "a:b.csv", "bill.exe"):
            with self.subTest(name=name), self.assertRaises(BillError):
                read_archives([archive(name=name)])
        with self.assertRaises(BillError):
            read_archives([b"not a zip"])
        with patch("reconciliation.gateway.MAX_UNPACKED", 5), self.assertRaises(BillError):
            read_archives([archive()])

    def test_header_only_still_requires_verified_schema(self):
        raw = read_archives([archive(rows=[])])
        self.assertEqual(normalize(raw, "TRADE_BILL", MERCHANT), [])
        with self.assertRaises(BillError):
            normalize([{"__header__": ["unknown"]}], "TRADE_BILL", MERCHANT)
        with self.assertRaises(BillError):
            normalize([trade(商户号="OTHER")], "TRADE_BILL", MERCHANT)
        with self.assertRaises(BillError):
            normalize([trade(请求日期="20260230")], "TRADE_BILL", MERCHANT)

    def test_mismatch_unknown_type_duplicate_and_missing_both_directions(self):
        def cats(rows, expected):
            return {i["category"] for i in compare(normalize(rows, "TRADE_BILL", MERCHANT), expected, "TRADE_BILL")[0]}
        self.assertEqual(cats([trade(交易金额="9.00")], [local()]), {"mismatch"})
        self.assertEqual(cats([trade(交易状态="处理中")], [local()]), {"unsupported"})
        self.assertEqual(cats([trade(交易类型="未支持的新类型")], [local()]), {"unsupported"})
        self.assertEqual(cats([trade(), trade()], [local()]), {"duplicate"})
        self.assertEqual(cats([trade()], []), {"missing_local"})
        self.assertEqual(cats([], [local()]), {"missing_channel"})
        self.assertEqual(cats([], [local(expected=False)]), set())
        self.assertEqual(cats([trade()], [local(fee=None)]), {"fee_unknown"})

    def test_refund_checks_original_transaction(self):
        rows = normalize([trade(交易类型="交易退款", 原汇付交易订单号="ORIGINAL")], "TRADE_BILL", MERCHANT)
        self.assertFalse(compare(rows, [local(kind="refund", original="ORIGINAL")], "TRADE_BILL")[0])
        self.assertEqual(compare(rows, [local(kind="refund", original="WRONG")], "TRADE_BILL")[0][0]["category"], "mismatch")

    def test_split_receivers_and_balance_fee_have_different_semantics(self):
        base = {"商户号": MERCHANT, "请求流水号": "REQ-1", "请求日期": "20261018",
                "入账方商户号": "RECEIVER", "分账金额": "10.00", "账务状态": "成功",
                "分账手续费": "0.00", "交易手续费": "0.06"}
        rows = normalize([base], "SPLIT_BILL", MERCHANT)
        expected = local(kind="split", route="balance", recipients={"RECEIVER": 1000})
        self.assertFalse(compare(rows, [expected], "SPLIT_BILL")[0])
        self.assertEqual(compare(rows, [{**expected, "route": "delayed"}], "SPLIT_BILL")[0][0]["category"], "mismatch")
        self.assertEqual(compare(rows * 2, [expected], "SPLIT_BILL")[0][0]["category"], "mismatch")

    def test_user_settlement_is_never_amount_only_match_for_withdrawal(self):
        rows = normalize([{"商户号": MERCHANT, "结算日期": "20261018", "结算金额": "10",
                           "结算交易状态": "成功", "手续费": "0"}], "SETTLE_USER_BILL", MERCHANT)
        issues, summary = compare(rows, [local()], "SETTLE_USER_BILL")
        self.assertEqual(issues[0]["category"], "settlement_manual")
        self.assertEqual(summary["matched_count"], 0)
        self.assertEqual(compare([], [], "WITHDRAWAL_BILL")[0], [])
        self.assertEqual(compare([], [local(expected=False)], "WITHDRAWAL_BILL")[0][0]["category"], "withdrawal_coverage")


@override_settings(**OPTIONS)
class GatewayTests(SimpleTestCase):
    def setUp(self):
        self.config = HuifuPaymentConfig.from_settings()

    def metadata(self, **changes):
        return {"resp_code": "00000000", "req_date": timezone.localdate().strftime("%Y%m%d"),
                "req_seq_id": "fixed", **changes}

    def test_actual_sdk_endpoint_and_signature_verified_before_file_download(self):
        def post(session, url, **kwargs):
            self.assertEqual(url, "https://api.huifu.com/v2/trade/check/filequery")
            body = kwargs["json"]["data"]
            self.assertEqual(body["bill_type"], "TRADE_BILL")
            self.assertEqual(body["file_date"], "20261019")
            self.assertEqual(body["huifu_id"], MERCHANT)
            return signed_http_response({
                "resp_code": "00000000", "req_date": body["req_date"], "req_seq_id": body["req_seq_id"],
                "file_details": [{"huifu_id": MERCHANT, "file_date": "20261019", "file_Name": "bill.zip",
                                  "bill_type": "TRADE_BILL", "download_url": "https://file.cloudpnr.com/private?secret=sample"}],
            })
        with patch("requests.sessions.Session.post", autospec=True, side_effect=post) as http, \
                patch("reconciliation.gateway.download_bill", return_value=archive()) as download:
            rows, digest = fetch_bill(self.config, DAY, "TRADE_BILL")
        self.assertEqual(http.call_count, 1)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(len(digest), 64)
        self.assertEqual(normalize(rows, "TRADE_BILL", MERCHANT)[0]["amount"], 1000)

    def test_empty_success_is_waiting_not_empty_day_except_completed_empty_task(self):
        with patch("reconciliation.gateway.uuid.uuid4") as ident, patch("reconciliation.gateway.sdk_call") as sdk:
            ident.return_value.hex = "fixed"
            sdk.return_value = self.metadata()
            with self.assertRaises(BillPending):
                fetch_bill(self.config, DAY, "TRADE_BILL")
            sdk.return_value = self.metadata(task_details=[{"huifu_id": MERCHANT, "bill_type": "TRADE_BILL",
                                                          "data_date": "20261018", "task_stat": "S"}])
            self.assertEqual(fetch_bill(self.config, DAY, "TRADE_BILL")[0], [])
            sdk.return_value["task_details"][0]["file_id"] = "STILL-MISSING"
            with self.assertRaises(BillPending):
                fetch_bill(self.config, DAY, "TRADE_BILL")

    def test_metadata_mismatch_or_transport_failure_never_downloads_or_leaks(self):
        with patch("reconciliation.gateway.uuid.uuid4") as ident, patch("reconciliation.gateway.sdk_call") as sdk, \
                patch("reconciliation.gateway.download_bill") as download:
            ident.return_value.hex = "fixed"
            sdk.return_value = self.metadata(req_seq_id="wrong")
            with self.assertRaises(BillError):
                fetch_bill(self.config, DAY, "TRADE_BILL")
            sdk.side_effect = RuntimeError("https://example.invalid/?secret=TOPSECRET")
            with self.assertRaises(BillError) as error:
                fetch_bill(self.config, DAY, "TRADE_BILL")
            self.assertNotIn("TOPSECRET", str(error.exception))
            download.assert_not_called()

    def test_partial_file_generation_does_not_pass_as_complete_bill(self):
        with patch("reconciliation.gateway.uuid.uuid4") as ident, patch("reconciliation.gateway.sdk_call") as sdk, \
                patch("reconciliation.gateway.download_bill") as download:
            ident.return_value.hex = "fixed"
            sdk.return_value = self.metadata(
                file_details=[{"huifu_id": MERCHANT, "file_date": "20261019", "file_name": "part.zip",
                               "bill_type": "TRADE_BILL", "download_url": "https://file.cloudpnr.com/part.zip"}],
                task_details=[{"huifu_id": MERCHANT, "bill_type": "TRADE_BILL",
                               "data_date": "20261018", "task_stat": "P"}],
            )
            with self.assertRaises(BillPending):
                fetch_bill(self.config, DAY, "TRADE_BILL")
            download.assert_not_called()

    def test_download_rejects_untrusted_or_private_destinations_without_network(self):
        with patch("requests.Session.get") as get:
            for url in ("http://file.cloudpnr.com/bill", "https://evil.example/bill",
                        "https://user:password@file.cloudpnr.com/bill", "https://file.cloudpnr.com:8443/bill"):
                with self.subTest(url=url), self.assertRaises(BillError):
                    download_bill(url)
            with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]), self.assertRaises(BillError):
                download_bill("https://file.cloudpnr.com/bill")
            get.assert_not_called()

    def test_download_is_bounded_and_redirects_are_not_followed(self):
        addresses = [(0, 0, 0, "", ("223.5.5.5", 443))]
        with patch("socket.getaddrinfo", return_value=addresses), patch("requests.Session") as session:
            client = session.return_value.__enter__.return_value
            response = client.get.return_value.__enter__.return_value
            response.status_code = 302
            with self.assertRaises(BillError):
                download_bill("https://file.cloudpnr.com/bill")
            self.assertIs(client.trust_env, False)
            self.assertFalse(client.get.call_args.kwargs["allow_redirects"])
            response.status_code = 200
            response.iter_content.return_value = [b"oversized"]
            with patch("reconciliation.gateway.MAX_DOWNLOAD", 3), self.assertRaises(BillError):
                download_bill("https://file.cloudpnr.com/bill")


@override_settings(**OPTIONS)
class BatchTests(TransactionTestCase):
    def setUp(self):
        self.config = HuifuPaymentConfig.from_settings()
        self.batch = Batch.objects.create(scope=_scope(self.config), data_date=DAY, bill_type="TRADE_BILL")
        self.time = patch("reconciliation.services.timezone.now", return_value=NOW)
        self.time.start()
        self.addCleanup(self.time.stop)
        self.net = patch("requests.sessions.Session.request", side_effect=AssertionError("No network"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def run_offline(self, *, rows=None, expected=None, error=None):
        with patch("reconciliation.services.fetch_bill", return_value=([trade()] if rows is None else rows, "a" * 64),
                   side_effect=error), patch("reconciliation.services.records_for", return_value=[local()] if expected is None else expected):
            return run_batch(self.batch.pk, config=self.config)

    def retry(self):
        Batch.objects.filter(pk=self.batch.pk).update(next_attempt_at=None)

    def test_success_repeated_tasks_do_not_repeat_fetch_or_issues(self):
        self.assertTrue(self.run_offline())
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, "matched")
        with patch("reconciliation.services.fetch_bill") as fetch:
            self.assertFalse(run_batch(self.batch.pk, config=self.config))
            fetch.assert_not_called()
        self.assertEqual(Issue.objects.count(), 0)

    def test_failure_preserves_previous_summary_and_creates_single_waiting_work(self):
        self.run_offline()
        self.retry()
        self.run_offline(error=BillPending("bill_not_ready"))
        self.retry()
        self.run_offline(error=BillPending("bill_not_ready"))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, "waiting")
        self.assertEqual(self.batch.summary["matched_count"], 1)
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(Event.objects.count(), 1)
        self.retry()
        with patch("reconciliation.services.fetch_bill") as fetch:
            self.assertFalse(run_batch(self.batch.pk, config=self.config))
            fetch.assert_not_called()

    def test_issues_idempotent_reopen_changed_evidence_and_clear_on_complete_only(self):
        item = finding("mismatch", "ORDER-1", local_amount=1000, channel_amount=900)
        with transaction.atomic():
            save_findings(self.batch, [item], complete=True)
            save_findings(self.batch, [item], complete=True)
        issue = Issue.objects.get()
        self.assertEqual(Event.objects.count(), 1)
        Issue.objects.filter(pk=issue.pk).update(status="resolved")
        with transaction.atomic():
            save_findings(self.batch, [item], complete=True)
        self.assertEqual(Issue.objects.get().status, "resolved")
        item["evidence"]["channel_amount"] = 800
        with transaction.atomic():
            save_findings(self.batch, [item], complete=True)
            save_findings(self.batch, [], complete=False)
        self.assertEqual(Issue.objects.get().status, "open")
        self.assertEqual(Issue.objects.get().revision, 2)
        with transaction.atomic():
            save_findings(self.batch, [], complete=True)
        self.assertEqual(Issue.objects.get().status, "cleared")

    def test_disabled_scope_current_date_and_outer_transaction_fail_closed(self):
        with override_settings(HUIFU_RECONCILIATION_ENABLED=False):
            self.assertFalse(run_batch(self.batch.pk))
        with transaction.atomic(), self.assertRaises(BillError):
            run_batch(self.batch.pk, config=self.config)
        for changes in ({"scope": "other"}, {"scope": _scope(self.config), "data_date": NOW.date()}):
            Batch.objects.filter(pk=self.batch.pk).update(**changes)
            with self.assertRaises(BillError):
                run_batch(self.batch.pk, config=self.config)

    def test_running_lease_and_stale_worker_cannot_overwrite_later_run(self):
        Batch.objects.filter(pk=self.batch.pk).update(lease_until=NOW + timedelta(minutes=2))
        self.assertFalse(self.run_offline())
        Batch.objects.filter(pk=self.batch.pk).update(lease_until=None)
        def delayed(*args):
            Batch.objects.filter(pk=self.batch.pk).update(run_token=uuid.uuid4())
            return [], "old"
        with patch("reconciliation.services.fetch_bill", side_effect=delayed):
            self.assertFalse(run_batch(self.batch.pk, config=self.config))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.evidence_digest, "")

    def test_scheduler_window_start_date_and_unique_daily_batches(self):
        with patch("reconciliation.services.run_batch", return_value=True) as run:
            self.assertEqual(daily()["checked"], 10)
            daily()
            self.assertEqual(Batch.objects.count(), 10)
            self.assertEqual(run.call_count, 20)
        with patch("reconciliation.services.timezone.now", return_value=NOW.replace(hour=17)):
            self.assertTrue(daily()["waiting_for_bill_window"])
        with override_settings(HUIFU_RECONCILIATION_START_DATE="invalid"):
            self.assertTrue(daily()["disabled"])
        self.assertEqual(len(ensure_day(DAY, self.config)), 5)


@override_settings(**OPTIONS)
class AdminTests(APITestCase):
    setUpTestData = classmethod(fixtures.BackofficeProviderReviewTests.setUpTestData.__func__)
    create_fulfillment_order = fixtures.BackofficeProviderReviewTests.create_fulfillment_order

    def setUp(self):
        self.client.force_authenticate(self.platform_admin)
        self.config = HuifuPaymentConfig.from_settings()
        self.batch = Batch.objects.create(scope=_scope(self.config), data_date=DAY, bill_type="TRADE_BILL")
        save_findings(self.batch, [finding("mismatch", "ORDER-1", local_amount=1000, channel_amount=900)], complete=True)
        self.issue = Issue.objects.get()
        self.net = patch("requests.sessions.Session.request", side_effect=AssertionError("No network"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def action(self, action, **changes):
        self.issue.refresh_from_db()
        return self.client.post(reverse("admin-reconciliation-issue", args=[self.issue.pk]), {
            "revision": self.issue.revision, "action": action, "note": "核对渠道原流水与账单", **changes,
        }, format="json")

    def test_city_permissions_cannot_expose_platform_bills(self):
        self.role.permissions += ["reconciliation.view", "reconciliation.manage", "reconciliation.review"]
        self.role.save()
        self.client.force_authenticate(self.admin_user)
        self.assertEqual(self.client.get(reverse("admin-reconciliation")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin-reconciliation-issue", args=[self.issue.pk])).status_code, 403)
        self.assertEqual(self.action("start").status_code, 403)
        work = self.client.get(reverse("admin-work-summary")).data["data"]
        self.assertNotIn("reconciliation_difference", [i["key"] for i in work["todos"]])

    def test_work_queue_read_and_recheck_do_not_repeat_unread(self):
        def todo():
            data = self.client.get(reverse("admin-work-summary")).data["data"]
            return next(x for x in data["todos"] if x["key"] == "reconciliation_difference")
        self.assertEqual(todo()["count"], 1)
        result = self.client.get(reverse("admin-work-items"), {"queue": "reconciliation_difference"}).data["data"]["items"][0]
        self.assertEqual(result["target"]["page"], "reconciliation")
        read = {k: result[k] for k in ("queue", "object_id", "event_version")}
        self.assertEqual(self.client.post(reverse("admin-work-items"), read, format="json").status_code, 200)
        self.assertEqual(todo()["unread_count"], 0)
        save_findings(self.batch, [finding("mismatch", "ORDER-1", local_amount=1000, channel_amount=900)], complete=True)
        self.assertEqual(todo()["unread_count"], 0)
        save_findings(self.batch, [finding("mismatch", "ORDER-1", local_amount=1000, channel_amount=800)], complete=True)
        self.assertEqual(todo()["unread_count"], 1)

    def test_two_person_review_revision_and_evidence_required(self):
        self.assertEqual(self.action("start").status_code, 200)
        self.assertEqual(self.action("submit").status_code, 400)
        self.assertEqual(self.action("submit", evidence_reference="FIN-20261018-1").status_code, 200)
        self.assertEqual(self.action("approve").status_code, 400)
        reviewer = get_user_model().objects.create_superuser(phone="19900009999", password="offline", nickname="复核财务")
        self.client.force_authenticate(reviewer)
        self.assertEqual(self.action("approve", revision=1).status_code, 400)
        result = self.action("approve")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["data"]["status"], "resolved")
        self.assertEqual(result.data["data"]["events"][-1]["actor"], "复核财务")
        self.assertEqual(Event.objects.count(), 4)

    def test_dates_pagination_batch_filter_and_secret_links(self):
        url = reverse("admin-reconciliation")
        result = self.client.get(url, {"view": "issues", "batch_id": str(self.batch.public_id)})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["data"]["total"], 1)
        self.assertEqual(self.client.get(url, {"view": "issues", "issue_status": "review"}).data["data"]["total"], 0)
        self.assertEqual(self.client.get(url, {"start": "2026-10-20", "end": "2026-10-18"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"page": "0"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"view": "issues", "work_id": "99999"}).data["data"]["total"], 0)
        self.assertEqual(self.action("start", note="复制 https://file.cloudpnr.com/bill?token=secret").status_code, 400)

    def test_projection_channel_portion_and_recharge_never_change_business_data(self):
        order = self.create_fulfillment_order(order_no="RECON-MIXED", provider=self.handan)
        payment = order.payment_order
        payment.gateway_merchant_id, payment.req_date, payment.req_seq_id = MERCHANT, "20261018", "REQ-MIXED"
        payment.save()
        WalletPaymentAllocation.objects.create(user=order.customer, business_type="provider_order",
                                              business_order_no=order.order_no, payable_amount=16800,
                                              wallet_amount=6800, external_amount=10000)
        recharge = WalletRechargeOrder.objects.create(
            user=order.customer, unit_face_amount=1000, quantity=1, credited_amount=1000,
            discount_rate_bps=10000, discount_amount=0, payable_amount=1000,
            gateway_merchant_id=MERCHANT, req_date="20261018", req_seq_id="REQ-RECHARGE",
            gateway_trade_no="RECHARGE-HF", status="paid", paid_at=NOW, expires_at=NOW,
        )
        rows = records_for(self.config, DAY, "TRADE_BILL", {("20261018", "REQ-MIXED"), ("20261018", "REQ-RECHARGE")})
        amounts = {r["key"][1]: r["amount"] for r in rows}
        self.assertEqual(amounts, {"REQ-MIXED": 10000, "REQ-RECHARGE": 1000})
        self.assertFalse(any(r["expected"] for r in rows))  # matched by request across days
        payment.refresh_from_db()
        recharge.refresh_from_db()
        self.assertEqual(payment.payable_amount, 16800)
        self.assertEqual(recharge.status, "paid")
