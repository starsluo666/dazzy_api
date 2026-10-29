"""Verify deployed defaults independently from test.py's explicit opt-out."""

import os
import subprocess
import sys

from django.test import SimpleTestCase


class ContentReviewDefaultTests(SimpleTestCase):
    def load(self, value=None):
        env = dict(os.environ)
        env.pop("CONTENT_REVIEW_ENABLED", None)
        env.pop("TENCENT_CONTENT_REVIEW_ENABLED", None)
        if value is not None:
            env["CONTENT_REVIEW_ENABLED"] = value
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from config.settings import base; print(base.CONTENT_REVIEW_ENABLED, base.TENCENT_CONTENT_REVIEW_ENABLED)",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        return result.stdout.strip()

    def test_review_is_enabled_by_default_without_starting_paid_calls(self):
        self.assertEqual(self.load(), "True False")

    def test_explicit_environment_opt_out_still_works(self):
        self.assertEqual(self.load("false"), "False False")
