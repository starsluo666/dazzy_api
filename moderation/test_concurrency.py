"""Real PostgreSQL locks, independent connections, and synthetic detector responses."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.access import resolve_admin_access
from backoffice.services import review_provider_profile_revision, review_provider_service_revision
from providers.models import ProviderCategoryGrant, ProviderServiceRevision
from providers.services import submit_provider_profile_revision, submit_provider_service_revision
from . import services
from .models import ContentReview as Review
from .tests import fixture, decide


PASS = {"suggestion": "Pass", "request_id": "synthetic-pass", "label": "Normal", "score": 0}
BLOCK = {"suggestion": "Block", "request_id": "synthetic-block", "label": "Porn", "score": 99}


def independent(function):
    connections.close_all()
    try:
        return function()
    finally:
        connections.close_all()


def parallel(*functions):
    start = Barrier(len(functions))

    def run(function):
        def action():
            start.wait(timeout=15)
            return function()

        return independent(action)

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(run, functions))


@override_settings(CONTENT_REVIEW_ENABLED=True, TENCENT_CONTENT_REVIEW_ENABLED=True)
class ContentConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires isolated PostgreSQL row locks and independent connections.")
        fixture(self)
        ProviderCategoryGrant.objects.create(
            provider=self.provider, category=self.category, is_active=True
        )
        self.provider.lifestyle_photo.provider_photo_cropped = True
        self.provider.lifestyle_photo.save()
        self.access = resolve_admin_access(self.admin)
        self.request = SimpleNamespace(headers={}, META={})

    def approve_profile(self, revision_id):
        try:
            review_provider_profile_revision(
                revision_id=revision_id,
                decision="approve",
                reason="",
                actor=self.admin,
                access=self.access,
                request=self.request,
            )
            return "approved"
        except ValidationError:
            return "blocked"

    def test_identical_submissions_create_one_review_and_one_set_of_items(self):
        def submit():
            return services.submit("nickname", self.user, candidate="并发提交昵称").pk

        results = parallel(submit, submit)
        self.assertEqual(results[0], results[1])
        review = Review.objects.get()
        self.assertEqual(review.items.count(), 1)
        self.assertEqual(review.events.count(), 1)

    def test_different_submissions_have_exactly_one_current_version(self):
        results = parallel(
            lambda: services.submit("nickname", self.user, candidate="第一份昵称").pk,
            lambda: services.submit("nickname", self.user, candidate="第二份昵称").pk,
        )
        self.assertNotEqual(results[0], results[1])
        self.assertEqual(Review.objects.filter(is_current=True).count(), 1)
        self.assertEqual(Review.objects.filter(is_current=False, status="superseded").count(), 1)
        self.assertEqual(sum(row.events.count() for row in Review.objects.all()), 3)

    def test_two_reviewers_cannot_apply_the_same_revision_twice(self):
        review = services.submit("nickname", self.user, candidate="待并发核实昵称")

        def act(action):
            try:
                return services.decide(
                    review.pk,
                    actor=self.admin,
                    action=action,
                    revision=1,
                    reason="已在独立连接逐项核实",
                ).status
            except ValidationError:
                return "blocked"

        results = parallel(lambda: act("approve"), lambda: act("reject"))
        self.assertEqual(results.count("blocked"), 1)
        review.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(review.revision, 2)
        self.assertEqual(review.events.count(), 2)
        self.assertEqual(
            self.user.nickname, "待并发核实昵称" if review.status == "approved" else "原昵称"
        )

    def test_two_workers_can_only_claim_one_lease(self):
        review = services.submit("nickname", self.user, candidate="等待领取昵称")
        results = parallel(lambda: services._claim(review.pk), lambda: services._claim(review.pk))
        self.assertEqual(sum(row is not None for row in results), 1)
        review.refresh_from_db()
        self.assertEqual(review.status, "processing")
        self.assertNotIn(review.pk, services.pending_ids())

    def test_overlapping_workers_send_one_detector_request_outside_transactions(self):
        review = services.submit("nickname", self.user, candidate="多任务检测昵称")
        entered, release = Event(), Event()
        pids = []

        def detect(*_):
            self.assertFalse(connection.in_atomic_block)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids.append(cursor.fetchone()[0])
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("Detector release timed out")
            return PASS

        with (
            patch("moderation.gateway.detect_text", side_effect=detect) as detector,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            first = pool.submit(independent, lambda: services.process_one(review.pk))
            try:
                self.assertTrue(entered.wait(timeout=15))
                self.assertFalse(services.process_one(review.pk))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=15))
            self.assertEqual(detector.call_count, 1)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            self.assertNotEqual(pids[0], cursor.fetchone()[0])
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "多任务检测昵称")

    def late_detection(self, during):
        review = services.submit("nickname", self.user, candidate="旧待检测昵称")
        entered, release = Event(), Event()

        def detect(*_):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("Detector release timed out")
            return PASS

        with (
            patch("moderation.gateway.detect_text", side_effect=detect),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(independent, lambda: services.process_one(review.pk))
            try:
                self.assertTrue(entered.wait(timeout=15))
                during(review)
            finally:
                release.set()
            future.result(timeout=15)
        review.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "原昵称")
        return review

    def test_new_submission_committed_during_detection_fences_the_old_worker(self):
        def during(_):
            services.submit("nickname", self.user, candidate="刚提交的新昵称")

        old = self.late_detection(during)
        self.assertFalse(old.is_current)
        self.assertEqual(old.status, "superseded")
        current = Review.objects.get(is_current=True)
        with patch("moderation.gateway.detect_text", return_value=PASS):
            services.process_one(current.pk)
        self.user.refresh_from_db()
        self.assertEqual(self.user.nickname, "刚提交的新昵称")

    def test_manual_rejection_committed_during_detection_fences_the_old_pass(self):
        def during(review):
            review.refresh_from_db()
            decide(review, self.admin, "reject")

        review = self.late_detection(during)
        self.assertEqual(review.status, "rejected")
        self.assertEqual(review.events.filter(action="machine_result").count(), 0)

    def test_expired_worker_cannot_overwrite_a_new_worker_result(self):
        review = services.submit("nickname", self.user, candidate="有效租约昵称")
        entered, release, counter_lock = Event(), Event(), Lock()
        calls = 0

        def detect(*_):
            nonlocal calls
            self.assertFalse(connection.in_atomic_block)
            with counter_lock:
                calls += 1
                own = calls
            if own == 1:
                entered.set()
                if not release.wait(timeout=15):
                    raise AssertionError("Detector release timed out")
                return BLOCK
            return PASS

        with (
            patch("moderation.gateway.detect_text", side_effect=detect),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            old = pool.submit(independent, lambda: services.process_one(review.pk))
            try:
                self.assertTrue(entered.wait(timeout=15))
                Review.objects.filter(pk=review.pk).update(
                    lease_until=timezone.now() - timedelta(seconds=1)
                )
                self.assertTrue(services.process_one(review.pk))
            finally:
                release.set()
            old.result(timeout=15)
        review.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(calls, 2)
        self.assertEqual(review.status, "approved")
        self.assertEqual(self.user.nickname, "有效租约昵称")
        self.assertEqual(review.events.filter(action="machine_result").count(), 1)

    def test_simultaneous_profile_submissions_keep_one_pending_business_revision(self):
        def submit(name):
            try:
                return submit_provider_profile_revision(
                    provider=self.provider, data={"display_name": name}
                ).pk
            except ValidationError:
                return None

        results = parallel(lambda: submit("第一份资料"), lambda: submit("第二份资料"))
        self.assertEqual(sum(row is not None for row in results), 1)
        self.assertEqual(self.provider.profile_revisions.filter(status="pending").count(), 1)
        self.assertEqual(Review.objects.filter(kind="profile_revision", is_current=True).count(), 1)

    def test_profile_publication_racing_takedown_cannot_release_the_hold(self):
        canonical = decide(services.submit("provider", self.provider), self.admin)
        revision = submit_provider_profile_revision(provider=self.provider, data={})
        decide(Review.objects.get(kind="profile_revision", target_id=revision.pk), self.admin)
        results = parallel(
            lambda: decide(canonical, self.admin, "withdraw").status,
            lambda: self.approve_profile(revision.pk),
        )
        self.assertIn("withdrawn", results)
        self.provider.refresh_from_db()
        self.assertTrue(self.provider.content_hidden)
        self.assertEqual(self.provider.content_hidden_review, canonical.public_id)

    def test_service_publication_racing_takedown_cannot_release_the_hold(self):
        canonical = decide(services.submit("service", self.service), self.admin)
        revision = submit_provider_service_revision(
            provider=self.provider, service=self.service, data={}
        )
        decide(Review.objects.get(kind="service_revision", target_id=revision.pk), self.admin)

        def approve():
            try:
                review_provider_service_revision(
                    revision_id=revision.pk,
                    decision="approve",
                    reason="",
                    actor=self.admin,
                    access=self.access,
                    request=self.request,
                )
                return "approved"
            except ValidationError:
                return "blocked"

        results = parallel(lambda: decide(canonical, self.admin, "withdraw").status, approve)
        self.assertIn("withdrawn", results)
        self.service.refresh_from_db()
        self.assertTrue(self.service.content_hidden)
        self.assertEqual(self.service.content_hidden_review, canonical.public_id)
        self.assertLessEqual(ProviderServiceRevision.objects.filter(status="approved").count(), 1)
