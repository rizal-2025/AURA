"""Safe CLI contract for the external-scheduler demo cleanup job."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.jobs import demo_cleanup
from app.services.demo_cleanup_service import DemoCleanupSummary
from app.services.demo_cleanup_service import DemoCleanupDryRunSummary


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

    def dry_run_once(self, *, batch_size):
        return DemoCleanupDryRunSummary(
            eligible_sessions=batch_size,
            eligible_messages=4,
            eligible_reservations=2,
            eligible_workflow_states=1,
            eligible_handoffs=1,
            eligible_session_buckets=2,
            eligible_expired_buckets=3,
            blocked_sessions=0,
        )


class _PartialFailureService(_Service):
    async def run_once(self, *, batch_size):
        return DemoCleanupSummary(
            scanned=batch_size,
            cleaned_sessions=2,
            skipped_locked=0,
            skipped_not_eligible=0,
            failed_sessions=1,
            deleted_expired_buckets=0,
        )


class _TotalFailureService(_Service):
    async def run_once(self, *, batch_size):
        return DemoCleanupSummary(
            scanned=batch_size,
            cleaned_sessions=0,
            skipped_locked=0,
            skipped_not_eligible=0,
            failed_sessions=batch_size,
            deleted_expired_buckets=0,
        )


class DemoCleanupJobTests(unittest.TestCase):
    def test_parser_defaults_to_single_run(self):
        values = demo_cleanup.build_parser().parse_args([])
        self.assertFalse(values.once)
        self.assertEqual(values.batch_size, 100)
        self.assertFalse(values.dry_run)

    def test_once_outputs_only_safe_aggregate_counts(self):
        with (
            patch.dict(
                "sys.modules",
                {"app.db.database": SimpleNamespace(SessionLocal=object())},
            ),
            patch(
                "app.core.config.get_environment_settings",
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

    def test_dry_run_outputs_only_safe_aggregate_counts(self):
        with (
            patch.dict(
                "sys.modules",
                {"app.db.database": SimpleNamespace(SessionLocal=object())},
            ),
            patch(
                "app.core.config.get_environment_settings",
                return_value=SimpleNamespace(APP_ENV="demo"),
            ),
            patch(
                "app.services.demo_cleanup_service.DemoCleanupService",
                _Service,
            ),
            patch("builtins.print") as output,
        ):
            result = demo_cleanup.main(
                ["--once", "--dry-run", "--batch-size", "7"]
            )
        self.assertEqual(result, 0)
        rendered = output.call_args.args[0]
        self.assertIn('"mode": "dry-run"', rendered)
        self.assertIn('"eligible_sessions": 7', rendered)
        self.assertIn('"eligible_messages": 4', rendered)
        self.assertIn('"eligible_reservations": 2', rendered)
        for forbidden in ("session_id", "customer", "token", "database"):
            self.assertNotIn(forbidden, rendered.casefold())

    def test_partial_session_failure_is_nonzero(self):
        with (
            patch.dict(
                "sys.modules",
                {"app.db.database": SimpleNamespace(SessionLocal=object())},
            ),
            patch(
                "app.core.config.get_environment_settings",
                return_value=SimpleNamespace(APP_ENV="demo"),
            ),
            patch(
                "app.services.demo_cleanup_service.DemoCleanupService",
                _PartialFailureService,
            ),
            patch("builtins.print") as output,
        ):
            result = demo_cleanup.main(["--once", "--batch-size", "3"])
        self.assertEqual(result, 2)
        rendered = output.call_args.args[0]
        self.assertIn("DEMO_CLEANUP_PARTIAL_FAILURE", rendered)
        self.assertIn('"successful_cleanup_count": 2', rendered)
        self.assertIn('"failed_cleanup_count": 1', rendered)

    def test_total_session_failure_is_nonzero(self):
        with (
            patch.dict(
                "sys.modules",
                {"app.db.database": SimpleNamespace(SessionLocal=object())},
            ),
            patch(
                "app.core.config.get_environment_settings",
                return_value=SimpleNamespace(APP_ENV="demo"),
            ),
            patch(
                "app.services.demo_cleanup_service.DemoCleanupService",
                _TotalFailureService,
            ),
            patch("builtins.print") as output,
        ):
            result = demo_cleanup.main(["--once", "--batch-size", "3"])
        self.assertEqual(result, 1)
        rendered = output.call_args.args[0]
        self.assertIn("DEMO_CLEANUP_FAILED", rendered)
        self.assertIn('"successful_cleanup_count": 0', rendered)
        self.assertIn('"failed_cleanup_count": 3', rendered)

    def test_non_demo_fails_closed_with_safe_output(self):
        with (
            patch(
                "app.core.config.get_environment_settings",
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
