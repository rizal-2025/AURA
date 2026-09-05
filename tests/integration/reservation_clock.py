"""Test-only default reservation time; explicit clocks retain authority."""
from datetime import datetime, timezone
from unittest.mock import patch

from app.utils.datetime_parser import current_local_datetime as _real_local_datetime

FIXTURE_NOW = datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc)  # 10:00 Jakarta


def install_reservation_clock(test_case, clock=None):
    """Freeze the two existing clock bindings for one unittest + worker threads.

    No validation is replaced. A caller's explicit clock still wins. Register
    cleanup before fixture setup so exceptions cannot leak a clock into a test.
    """
    default_clock = clock or (lambda: FIXTURE_NOW)

    def local_now(*, clock=None):
        return _real_local_datetime(clock=clock or default_clock)

    for target in (
        "app.utils.datetime_parser.current_local_datetime",
        "app.services.reservation.service.current_local_datetime",
    ):
        test_case.enterContext(patch(target, side_effect=local_now))
