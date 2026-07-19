import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agents.orchestrator import AgentOrchestrator
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.classifier import IntentClassifier
from app.brain.memory_manager import MemoryManager
from app.db.models.reservation import Reservation
from app.db.repositories.reservation_repository import ReservationRepository


class FakeReservationService:
    def __init__(self):
        self.reservations = {
            2: SimpleNamespace(
                id=2,
                name="Rizal",
                people=4,
                date="2026-07-20",
                time="19:00",
                status="pending",
            ),
            1: SimpleNamespace(
                id=1,
                name="Budi",
                people=2,
                date="2026-07-21",
                time="20:00",
                status="pending",
            ),
        }
        self.update_calls = []

    def list_recent_reservations(self, db, limit=5):
        return sorted(self.reservations.values(), key=lambda item: item.id, reverse=True)[:limit]

    def get_reservation_by_id(self, db, reservation_id):
        return self.reservations.get(reservation_id)

    def update_reservation_field(self, db, reservation_id, field_name, new_value):
        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            return None
        setattr(reservation, field_name, new_value)
        self.update_calls.append((reservation_id, field_name, new_value))
        return reservation


class FakeUpdateQuery:
    def __init__(self, reservation):
        self.reservation = reservation

    def filter(self, clause):
        self.filter_clause = clause
        return self

    def first(self):
        return self.reservation


class TestUpdateReservation(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryManager()
        self.service = FakeReservationService()
        self.agent = UpdateReservationAgent(
            memory_manager=self.memory,
            reservation_service=self.service,
        )
        self.db = MagicMock()
        self.session_id = "update-session"

    def _send(self, message):
        return asyncio.run(self.agent.run(self.db, self.session_id, message))

    def _start_and_select_reservation(self, reservation_id="2"):
        self._send("ubah reservasi saya")
        return self._send(reservation_id)

    def test_successful_update(self):
        self._start_and_select_reservation()
        self._send("name")

        result = self._send("Andi")

        session = self.memory.get_session(self.session_id)
        self.assertEqual(self.service.reservations[2].name, "Andi")
        self.assertEqual(self.service.update_calls[-1], (2, "name", "Andi"))
        self.assertEqual(result["status"], "updated")
        self.assertIn("Reservasi berhasil diperbarui", result["response"])
        self.assertIsNone(session["update_reservation_stage"])
        self.assertIsNone(session["editing_field"])

    def test_invalid_reservation_id(self):
        self._send("ubah reservasi saya")

        result = self._send("999")

        session = self.memory.get_session(self.session_id)
        self.assertIn("ID reservasi tidak ditemukan", result["response"])
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.SELECT_RESERVATION_ID,
        )
        self.assertEqual(self.service.update_calls, [])

    def test_invalid_field(self):
        self._start_and_select_reservation()

        result = self._send("table")

        session = self.memory.get_session(self.session_id)
        self.assertIn("Field tidak valid", result["response"])
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.SELECT_FIELD,
        )
        self.assertEqual(self.service.update_calls, [])

    def test_update_people(self):
        self._start_and_select_reservation()
        self._send("people")

        result = self._send("7")

        self.assertEqual(self.service.reservations[2].people, 7)
        self.assertEqual(self.service.update_calls[-1], (2, "people", 7))
        self.assertIn("Jumlah Orang: 7", result["response"])

    def test_invalid_people_values_keep_update_session_active(self):
        self._start_and_select_reservation()
        self._send("people")

        for invalid_value in ("abc", "", "-7", "0", "7.5"):
            with self.subTest(value=invalid_value):
                result = self._send(invalid_value)
                session = self.memory.get_session(self.session_id)

                self.assertEqual(result["status"], "awaiting_update")
                self.assertEqual(
                    result["response"],
                    "Jumlah orang harus berupa angka positif. "
                    "Silakan masukkan jumlah orang yang valid.",
                )
                self.assertEqual(session["reservation_id"], 2)
                self.assertEqual(
                    session["update_reservation_stage"],
                    UpdateReservationAgent.INPUT_VALUE,
                )
                self.assertEqual(session["editing_field"], "people")
                self.assertEqual(self.service.reservations[2].people, 4)
                self.assertEqual(self.service.update_calls, [])

    def test_retry_after_invalid_people_value_keeps_update_session_active(self):
        self._start_and_select_reservation()
        self._send("jumlah orang")
        self._send("abc")

        result = self._send("-3")
        session = self.memory.get_session(self.session_id)

        self.assertEqual(result["status"], "awaiting_update")
        self.assertEqual(session["reservation_id"], 2)
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.INPUT_VALUE,
        )
        self.assertEqual(session["editing_field"], "people")
        self.assertEqual(self.service.update_calls, [])

    def test_successful_retry_with_valid_people_value(self):
        self._start_and_select_reservation()
        self._send("people")
        self._send("abc")

        result = self._send("7")
        session = self.memory.get_session(self.session_id)

        self.assertEqual(result["status"], "updated")
        self.assertEqual(self.service.reservations[2].people, 7)
        self.assertEqual(self.service.update_calls, [(2, "people", 7)])
        self.assertEqual(session["reservation_id"], 2)
        self.assertIsNone(session["update_reservation_stage"])
        self.assertIsNone(session["editing_field"])

    def test_reservation_id_persists_during_update_flow(self):
        self._start_and_select_reservation()
        session = self.memory.get_session(self.session_id)
        self.assertEqual(session["reservation_id"], 2)
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.SELECT_FIELD,
        )

        self._send("jumlah orang")
        self.assertEqual(session["reservation_id"], 2)
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.INPUT_VALUE,
        )
        self.assertEqual(session["editing_field"], "people")

        self._send("abc")
        self.assertEqual(session["reservation_id"], 2)
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.INPUT_VALUE,
        )
        self.assertEqual(session["editing_field"], "people")

    def test_orchestrator_logs_state_after_reservation_selection(self):
        orchestrator = AgentOrchestrator()
        orchestrator.memory_manager = self.memory
        orchestrator.update_reservation_agent = self.agent

        asyncio.run(
            orchestrator.handle(self.session_id, "ubah reservasi saya", self.db)
        )

        with patch("app.agents.orchestrator.logger") as mocked_logger:
            asyncio.run(orchestrator.handle(self.session_id, "2", self.db))

        mocked_logger.info.assert_any_call(
            "UPDATE RESERVATION STATE: session_id=%s status=%s "
            "reservation_id=%s stage=%s editing_field=%s",
            self.session_id,
            "awaiting_update",
            2,
            UpdateReservationAgent.SELECT_FIELD,
            None,
        )

    def test_update_date(self):
        self._start_and_select_reservation()
        self._send("date")

        result = self._send("2026-07-25")

        self.assertEqual(self.service.reservations[2].date, "2026-07-25")
        self.assertEqual(self.service.update_calls[-1], (2, "date", "2026-07-25"))
        self.assertIn("Tanggal: 2026-07-25", result["response"])

    def test_update_time(self):
        self._start_and_select_reservation()
        self._send("time")

        result = self._send("jam 8 malam")

        self.assertEqual(self.service.reservations[2].time, "20:00")
        self.assertEqual(self.service.update_calls[-1], (2, "time", "20:00"))
        self.assertIn("Jam: 20:00", result["response"])

    def test_repository_updates_allowed_field_and_rejects_invalid_field(self):
        reservation = SimpleNamespace(
            id=2,
            name="Rizal",
            people=4,
            date="2026-07-20",
            time="19:00",
            status="pending",
        )
        query = FakeUpdateQuery(reservation)
        db = MagicMock()
        db.query.return_value = query
        repository = ReservationRepository()

        updated = repository.update_reservation_field(db, 2, "people", 7)

        self.assertIs(updated, reservation)
        self.assertEqual(reservation.people, 7)
        db.commit.assert_called_once()
        db.refresh.assert_called_once_with(reservation)
        with self.assertRaises(ValueError):
            repository.update_reservation_field(db, 2, "table", "A1")

    def test_orchestrator_routes_classified_update_intent(self):
        orchestrator = AgentOrchestrator()
        db = MagicMock()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "update_reservation", "confidence": 0.95}

        class DummyUpdateReservationAgent:
            async def run(self, received_db, received_session_id, received_message):
                self.args = (received_db, received_session_id, received_message)
                return {"status": "awaiting_update", "response": "Pilih ID reservasi"}

        handler = DummyUpdateReservationAgent()
        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.update_reservation_agent = handler

        response = asyncio.run(
            orchestrator.handle("update-new-session", "ubah booking saya", db)
        )

        self.assertEqual(response, "Pilih ID reservasi")
        self.assertEqual(handler.args, (db, "update-new-session", "ubah booking saya"))

    def test_orchestrator_keeps_confirmation_priority_over_update_state(self):
        orchestrator = AgentOrchestrator()
        db = MagicMock()
        session_id = "confirmation-priority"
        session = orchestrator.memory_manager.get_session(session_id)
        session.update(
            {
                "intent": "reservation",
                "awaiting_confirmation": True,
                "update_reservation_stage": UpdateReservationAgent.SELECT_FIELD,
            }
        )

        class DummyWorkflow:
            async def execute(self, *args, **kwargs):
                return {
                    "status": "awaiting_confirmation",
                    "response": "Ditangani confirmation flow",
                }

        class FailingUpdateReservationAgent:
            async def run(self, *args, **kwargs):
                raise AssertionError("Update flow must not replace confirmation flow")

        orchestrator.workflow = DummyWorkflow()
        orchestrator.update_reservation_agent = FailingUpdateReservationAgent()

        response = asyncio.run(orchestrator.handle(session_id, "Ya", db))

        self.assertEqual(response, "Ditangani confirmation flow")

    def test_classifier_declares_update_reservation_intent(self):
        self.assertIn(
            "update_reservation",
            IntentClassifier._get_supported_intents(IntentClassifier()),
        )


if __name__ == "__main__":
    unittest.main()
