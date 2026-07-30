"""Safe CLI contract for the external-scheduler demo cleanup job."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.jobs import demo_cleanup
from app.services.demo_cleanup_service import DemoCleanupSummary


class _Service:
    def __init__(self, **values):
        self.values = values

    async def run_once(self, *, batch_size):
        return DemoCleanupSummary(
            scanned=batch_size,
            cleaned_sessions=2,
            skipped_locked=1,
            skipped_not_eligible=0,
            failed_sessions=0,
            deleted_expired_buckets=3,
        )


class DemoCleanupJobTests(unittest.TestCase):
    def test_parser_defaults_to_single_run(self):
        values = demo_cleanup.build_parser().parse_args([])
        self.assertFalse(values.once)
        self.assertEqual(values.batch_size, 100)

    def test_once_outputs_only_safe_aggregate_counts(self):
        with (
            patch(
                "app.core.config.get_application_settings",
                return_value=SimpleNamespace(APP_ENV="demo"),
            ),
            patch(
                "app.services.demo_cleanup_service.DemoCleanupService",
                _Service,
            ),
            patch("builtins.print") as output,
        ):
            result = demo_cleanup.main(["--once", "--batch-size", "7"])
        self.assertEqual(result, 0)
        rendered = output.call_args.args[0]
        self.assertIn('"scanned": 7', rendered)
        self.assertIn('"cleaned_sessions": 2', rendered)
        for forbidden in ("session_id", "customer", "token", "database"):
            self.assertNotIn(forbidden, rendered.casefold())

    def test_non_demo_fails_closed_with_safe_output(self):
        with (
            patch(
                "app.core.config.get_application_settings",
                return_value=SimpleNamespace(APP_ENV="production"),
            ),
            patch("builtins.print") as output,
        ):
            result = demo_cleanup.main(["--once"])
        self.assertEqual(result, 1)
        self.assertIn("DEMO_CLEANUP_FAILED", output.call_args.args[0])

    def test_invalid_batch_size_is_nonzero(self):
        with patch("builtins.print") as output:
            result = demo_cleanup.main(["--once", "--batch-size", "501"])
        self.assertEqual(result, 1)
        self.assertIn("DEMO_CLEANUP_FAILED", output.call_args.args[0])
