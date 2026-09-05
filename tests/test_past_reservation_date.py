import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.agents.reservation_agent import ReservationAgent
from app.brain.memory_manager import MemoryManager
from app.core.locale import SupportedLocale, presentation_locale
from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.reservation import ReservationCreate
from app.services.reservation.errors import PastReservationDateError
from app.services.reservation.service import ReservationService


FROZEN_NOW = datetime(2026, 9, 5, 5, 0, tzinfo=timezone.utc)
OWNER_ID = uuid4()
REFERENCE = "RSV_90909090909090909090909090909090"


def frozen_clock():
    return FROZEN_NOW


def create_data(reservation_date: str) -> ReservationCreate:
    return ReservationCreate(
        name="Juli",
        people=9,
        date=reservation_date,
        time="20:00",
    )


class ReservationDateDomainTests(unittest.TestCase):
    def setUp(self):
        self.repository = MagicMock()
        self.service = ReservationService(
            repository=self.repository,
            clock=frozen_clock,
        )

    def test_required_past_dates_are_rejected(self):
        for value in (
            "2026-09-04",
            "2026-08-31",
            "2026-07-09",
            "2025-12-31",
        ):
            with self.subTest(value=value):
                with self.assertRaises(PastReservationDateError):
                    self.service.validate_new_reservation_date(value)

    def test_today_and_future_dates_pass_date_policy(self):
        for value in (
            "2026-09-05",
            "2026-09-06",
            "2026-10-01",
            "2027-01-01",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self.service.validate_new_reservation_date(value),
                    value,
                )

    def test_year_boundary_uses_jakarta_calendar_date(self):
        service = ReservationService(
            repository=self.repository,
            clock=lambda: datetime(
                2025,
                12,
                31,
                17,
                30,
                tzinfo=timezone.utc,
            ),
        )
        with self.assertRaises(PastReservationDateError):
            service.validate_new_reservation_date("2025-12-31")
        self.assertEqual(
            service.validate_new_reservation_date("2026-01-01"),
            "2026-01-01",
        )
        self.assertEqual(
            service.validate_new_reservation_date("2026-01-02"),
            "2026-01-02",
        )

    def test_leap_day_comparison_remains_calendar_correct(self):
        service = ReservationService(
            repository=self.repository,
            clock=lambda: datetime(
                2028,
                2,
                28,
                17,
                0,
                tzinfo=timezone.utc,
            ),
        )
        with self.assertRaises(PastReservationDateError):
            service.validate_new_reservation_date("2028-02-28")
        self.assertEqual(
            service.validate_new_reservation_date("2028-02-29"),
            "2028-02-29",
        )

    def test_direct_create_bypass_is_rejected_before_any_mutation(self):
        before_mutation = MagicMock()
        db = MagicMock()
        repository = ReservationRepository()
        service = ReservationService(
            repository=repository,
            clock=frozen_clock,
        )
        untrusted = ReservationCreate.model_construct(
            name="Juli",
            people=9,
            date="2026-07-09",
            time="20:00",
        )

        with (
            patch.object(
                repository,
                "create",
                wraps=repository.create,
            ) as repository_create,
            patch(
                "app.db.repositories.reservation_repository."
                "generate_public_reference"
            ) as generate_reference,
            self.assertRaises(PastReservationDateError),
        ):
            service.create_reservation(
                db,
                untrusted,
                owner_customer_id=OWNER_ID,
                before_mutation=before_mutation,
            )

        before_mutation.assert_not_called()
        repository_create.assert_not_called()
        generate_reference.assert_not_called()
        db.add.assert_not_called()
        db.flush.assert_not_called()
        db.commit.assert_not_called()

    def test_valid_create_runs_mutation_marker_and_repository(self):
        before_mutation = MagicMock()
        self.repository.create.return_value = SimpleNamespace(
            id=1,
            name="Juli",
            people=9,
            date="2026-09-05",
            time="20:00",
            status="pending",
            public_reference=REFERENCE,
        )

        result = self.service.create_reservation(
            MagicMock(),
            create_data("2026-09-05"),
            owner_customer_id=OWNER_ID,
            before_mutation=before_mutation,
        )

        before_mutation.assert_called_once_with()
        self.repository.create.assert_called_once()
        self.assertEqual(result.reference, REFERENCE)


class ReservationDateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryManager()
        self.agent = ReservationAgent(
            memory_manager=self.memory,
            clock=frozen_clock,
        )
        self.agent.reservation_service.repository = MagicMock()

    def _run(self, session_id: str, message: str, *, db=None):
        return asyncio.run(
            self.agent.run(
                [{"action": "collect_missing_fields"}],
                self.memory.get_session(session_id),
                message,
                session_id=session_id,
                owner_customer_id=OWNER_ID,
                db=db,
            )
        )

    def _seed_date_prompt(self, session_id: str):
        self.memory.update_session(
            session_id,
            {
                "intent": "reservation",
                "name": "Juli",
                "people": 9,
                "date": None,
                "time": "20:00",
                "completed": False,
                "awaiting_confirmation": False,
                "asked_fields": ["name", "people", "date"],
            },
        )

    def test_natural_past_date_is_rejected_and_fields_are_preserved(self):
        self._seed_date_prompt("past-natural")

        result = self._run("past-natural", "9 Juli 2026")

        state = self.memory.get_session("past-natural")
        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(result["field"], "date")
        self.assertEqual(
            result["response"],
            "Tanggal reservasi tersebut sudah lewat. Silakan pilih tanggal "
            "hari ini atau tanggal setelahnya.",
        )
        self.assertEqual(state["name"], "Juli")
        self.assertEqual(state["people"], 9)
        self.assertEqual(state["time"], "20:00")
        self.assertIsNone(state.get("date"))
        self.agent.reservation_service.repository.create.assert_not_called()

    def test_future_correction_continues_and_can_create(self):
        self._seed_date_prompt("corrected")
        rejected = self._run("corrected", "9 Juli 2026")
        self.assertEqual(rejected["status"], "awaiting_input")

        confirmation = self._run("corrected", "6 September 2026")
        self.assertEqual(confirmation["status"], "awaiting_confirmation")
        self.assertIn("Tanggal: 6 September 2026", confirmation["response"])

        self.agent.reservation_service.repository.create.return_value = (
            SimpleNamespace(
                id=1,
                name="Juli",
                people=9,
                date="2026-09-06",
                time="20:00",
                status="pending",
                public_reference=REFERENCE,
            )
        )
        created = self._run("corrected", "ya", db=MagicMock())

        self.assertEqual(created["status"], "completed")
        self.assertIn("Reservasi berhasil dibuat", created["response"])
        self.agent.reservation_service.repository.create.assert_called_once()

    def test_confirmation_bypass_rejects_without_mutation_marker(self):
        workflow_state = MagicMock()
        agent = ReservationAgent(
            memory_manager=self.memory,
            workflow_state_service=workflow_state,
            clock=frozen_clock,
        )
        agent.reservation_service.repository = MagicMock()
        self.memory.update_session(
            "bypass",
            {
                "intent": "reservation",
                "name": "Juli",
                "people": 9,
                "date": "2026-07-09",
                "time": "20:00",
                "completed": False,
                "awaiting_confirmation": True,
                "asked_fields": ["name", "people", "date", "time"],
            },
        )

        result = asyncio.run(
            agent.handle_confirmation(
                "ya",
                "bypass",
                owner_customer_id=OWNER_ID,
                db=MagicMock(),
            )
        )

        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertNotIn("reservation_operation", result)
        self.assertIsNone(self.memory.get_session("bypass").get("date"))
        self.assertEqual(
            self.memory.get_session("bypass")["editing_field"],
            "date",
        )
        workflow_state.begin_mutation.assert_not_called()
        agent.reservation_service.repository.create.assert_not_called()

    def test_english_locale_uses_localized_recovery_message(self):
        self._seed_date_prompt("english")
        with presentation_locale(SupportedLocale.EN_US):
            result = self._run("english", "July 9, 2026")
        self.assertEqual(
            result["response"],
            "That reservation date has already passed. Please choose today "
            "or a future date.",
        )


if __name__ == "__main__":
    unittest.main()
