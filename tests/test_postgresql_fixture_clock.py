"""Freezing historical fixtures must not bypass reservation validation."""
import unittest
from datetime import datetime, timezone

from app.services.reservation.service import ReservationService
from app.services.reservation.errors import PastReservationDateError, PastReservationTimeError
from app.utils.datetime_parser import DatetimeParser
from tests.integration.reservation_clock import install_reservation_clock


class FixtureClockTests(unittest.TestCase):
    def test_default_clock_is_frozen_but_domain_guards_remain(self):
        install_reservation_clock(self)
        self.assertEqual(DatetimeParser.parse_date("today"), "2026-08-01")
        service = ReservationService()
        with self.assertRaises(PastReservationDateError):
            service.validate_new_reservation_datetime("2026-07-31", "20:00")
        with self.assertRaises(PastReservationTimeError):
            service.validate_new_reservation_datetime("2026-08-01", "09:59")
        service.validate_new_reservation_datetime("2026-08-01", "10:00")

    def test_explicit_clock_is_not_overridden(self):
        install_reservation_clock(self)
        clock = lambda: datetime(2026, 9, 5, 9, 11, tzinfo=timezone.utc)
        self.assertEqual(DatetimeParser.parse_date("today", clock=clock), "2026-09-05")
        with self.assertRaises(PastReservationDateError):
            ReservationService(clock=clock).validate_new_reservation_datetime("2026-08-01", "20:00")

    def test_fixture_clock_cleanup_restores_bindings(self):
        from app.utils import datetime_parser
        from app.services.reservation import service
        old_parser, old_service = datetime_parser.current_local_datetime, service.current_local_datetime
        scope = unittest.TestCase()
        install_reservation_clock(scope)
        scope.doCleanups()
        self.assertIs(datetime_parser.current_local_datetime, old_parser)
        self.assertIs(service.current_local_datetime, old_service)
