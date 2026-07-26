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
    OWNER_ID = "update-session"

    def __init__(self):
        self.reservations = {
            3: SimpleNamespace(
                id=3,
                name="Customer Lain",
                people=6,
                date="2026-07-22",
                time="18:00",
                status="pending",
                owner_customer_id="other-customer",
            ),
            2: SimpleNamespace(
                id=2,
                name="Rizal",
                people=4,
                date="2026-07-20",
                time="19:00",
                status="pending",
                owner_customer_id=self.OWNER_ID,
            ),
            1: SimpleNamespace(
                id=1,
                name="Budi",
                people=2,
                date="2026-07-21",
                time="20:00",
                status="pending",
                owner_customer_id=self.OWNER_ID,
            ),
        }
        self.update_calls = []

    def list_recent_reservations(self, db, owner_customer_id, limit=5):
        reservations = [
            reservation
            for reservation in self.reservations.values()
            if reservation.owner_customer_id == owner_customer_id
        ]
        return sorted(reservations, key=lambda item: item.id, reverse=True)[:limit]

    def get_reservation_by_id(self, db, reservation_id, owner_customer_id):
        reservation = self.reservations.get(reservation_id)
        if reservation is None or reservation.owner_customer_id != owner_customer_id:
            return None
        return reservation

    def update_reservation_field(
        self,
        db,
        reservation_id,
        field_name,
        new_value,
        owner_customer_id,
    ):
        reservation = self.get_reservation_by_id(db, reservation_id, owner_customer_id)
        if reservation is None:
            return None
        setattr(reservation, field_name, new_value)
        self.update_calls.append((reservation_id, field_name, new_value, owner_customer_id))
        return reservation


class FakeUpdateQuery:
    def __init__(self, reservation):
        self.reservation = reservation
        self.filter_clauses = []

    def filter(self, clause):
        self.filter_clauses.append(clause)
        return self

    def first(self):
        return self.reservation


class FakeAtomicResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


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
        return asyncio.run(
            self.agent.run(
                self.db,
                self.session_id,
                message,
                self.service.OWNER_ID,
            )
        )

    def _start_and_select_reservation(self, reservation_id="2"):
        self._send("ubah reservasi saya")
        return self._send(reservation_id)

    def test_successful_update(self):
        self._start_and_select_reservation()
        self._send("name")

        result = self._send("Andi")

        session = self.memory.get_session(self.session_id)
        self.assertEqual(self.service.reservations[2].name, "Andi")
        self.assertEqual(
            self.service.update_calls[-1],
            (2, "name", "Andi", self.service.OWNER_ID),
        )
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
        self.assertEqual(
            self.service.update_calls[-1],
            (2, "people", 7, self.service.OWNER_ID),
        )
        self.assertIn("Jumlah Orang: 7", result["response"])

    def test_people_value_accepts_natural_positive_integer_forms(self):
        for value in ("9", "9 orang", "menjadi 9 orang", "ubah jadi 9"):
            with self.subTest(value=value):
                self.assertEqual(self.agent._parse_new_value("people", value), 9)

    def test_update_people_with_natural_value(self):
        self._start_and_select_reservation()
        self._send("people")

        result = self._send("menjadi 9 orang")

        self.assertEqual(self.service.reservations[2].people, 9)
        self.assertEqual(
            self.service.update_calls[-1],
            (2, "people", 9, self.service.OWNER_ID),
        )
        self.assertEqual(result["status"], "updated")

    def test_invalid_people_values_keep_update_session_active(self):
        self._start_and_select_reservation()
        self._send("people")

        for invalid_value in (
            "abc",
            "",
            "0",
            "21",
            "-5",
            "3.5",
            "9 dan 10",
        ):
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
        self.assertEqual(
            self.service.update_calls,
            [(2, "people", 7, self.service.OWNER_ID)],
        )
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

    def test_natural_selection_phrases_choose_existing_reservation(self):
        for index, message in enumerate(
            (
                "yang nomor dua",
                "reservasi nomor 2",
                "booking yang kedua",
                "pesanan saya yang nomor dua",
            )
        ):
            with self.subTest(message=message):
                memory = MemoryManager()
                service = FakeReservationService()
                agent = UpdateReservationAgent(
                    memory_manager=memory,
                    reservation_service=service,
                )
                session_id = f"natural-update-selection-{index}"
                asyncio.run(
                    agent.run(
                        self.db,
                        session_id,
                        "ubah reservasi saya",
                        service.OWNER_ID,
                    )
                )
                result = asyncio.run(
                    agent.run(
                        self.db,
                        session_id,
                        message,
                        service.OWNER_ID,
                    )
                )

                session = memory.get_session(session_id)
                self.assertEqual(session["reservation_id"], 2)
                self.assertEqual(
                    session["update_reservation_stage"],
                    UpdateReservationAgent.SELECT_FIELD,
                )
                self.assertIn("Reservasi dipilih", result["response"])

    def test_natural_field_corrections_resolve_only_allowlisted_fields(self):
        cases = {
            "jamnya ganti": "time",
            "tanggalnya pindah": "date",
            "orangnya ditambah": "people",
            "orangnya dikurangi": "people",
            "namanya mau diganti": "name",
            "ganti hari": "date",
            "jadinya tiga orang": "people",
            "jamnya jadi delapan malam": "time",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(self.agent._resolve_field(message), expected)
        self.assertIsNone(self.agent._resolve_field("ganti status"))

    def test_orchestrator_logs_state_after_reservation_selection(self):
        orchestrator = AgentOrchestrator()
        orchestrator.memory_manager = self.memory
        orchestrator.update_reservation_agent = self.agent

        asyncio.run(
            orchestrator.handle(
                self.session_id,
                "ubah reservasi saya",
                self.db,
                owner_customer_id=self.service.OWNER_ID,
            )
        )

        with patch("app.agents.orchestrator.logger") as mocked_logger:
            asyncio.run(
                orchestrator.handle(
                    self.session_id,
                    "2",
                    self.db,
                    owner_customer_id=self.service.OWNER_ID,
                )
            )

        mocked_logger.info.assert_any_call(
            "UPDATE RESERVATION STATE: status=%s stage=%s editing_field=%s",
            "awaiting_update",
            UpdateReservationAgent.SELECT_FIELD,
            None,
        )

    def test_update_date(self):
        self._start_and_select_reservation()
        self._send("date")

        result = self._send("2026-07-25")

        self.assertEqual(self.service.reservations[2].date, "2026-07-25")
        self.assertEqual(
            self.service.update_calls[-1],
            (2, "date", "2026-07-25", self.service.OWNER_ID),
        )
        self.assertIn("Tanggal: 2026-07-25", result["response"])

    def test_update_time(self):
        self._start_and_select_reservation()
        self._send("time")

        result = self._send("jam 8 malam")

        self.assertEqual(self.service.reservations[2].time, "20:00")
        self.assertEqual(
            self.service.update_calls[-1],
            (2, "time", "20:00", self.service.OWNER_ID),
        )
        self.assertIn("Jam: 20:00", result["response"])

    def test_repository_updates_allowed_field_and_rejects_invalid_field(self):
        reservation = SimpleNamespace(
            id=2,
            name="Rizal",
            people=4,
            date="2026-07-20",
            time="19:00",
            status="pending",
            owner_customer_id=self.service.OWNER_ID,
        )
        db = MagicMock()
        db.execute.return_value = FakeAtomicResult(reservation)
        repository = ReservationRepository()

        updated = repository.update_reservation_field(
            db,
            2,
            "people",
            7,
            self.service.OWNER_ID,
        )

        self.assertIs(updated, reservation)
        db.commit.assert_not_called()
        db.rollback.assert_not_called()
        db.refresh.assert_not_called()
        statement = db.execute.call_args.args[0]
        self.assertIn("reservations.id", str(statement))
        self.assertIn("owner_customer_id", str(statement))
        with self.assertRaises(ValueError):
            repository.update_reservation_field(
                db,
                2,
                "table",
                "A1",
                self.service.OWNER_ID,
            )

    def test_repository_atomic_update_rejects_non_owner(self):
        db = MagicMock()
        db.execute.return_value = FakeAtomicResult(None)
        repository = ReservationRepository()

        updated = repository.update_reservation_field(
            db,
            3,
            "people",
            7,
            self.service.OWNER_ID,
        )

        self.assertIsNone(updated)
        db.commit.assert_not_called()
        statement = db.execute.call_args.args[0]
        self.assertIn("reservations.id", str(statement))
        self.assertIn("owner_customer_id", str(statement))

    def test_user_cannot_update_another_customers_reservation(self):
        self._send("ubah reservasi saya")

        result = self._send("3")
        session = self.memory.get_session(self.session_id)

        self.assertIn("ID reservasi tidak ditemukan", result["response"])
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.SELECT_RESERVATION_ID,
        )
        self.assertEqual(self.service.reservations[3].people, 6)
        self.assertEqual(self.service.update_calls, [])

    def test_orchestrator_routes_classified_update_intent(self):
        orchestrator = AgentOrchestrator()
        db = MagicMock()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "update_reservation", "confidence": 0.95}

        class DummyUpdateReservationAgent:
            async def run(
                self,
                received_db,
                received_session_id,
                received_message,
                received_owner_customer_id,
            ):
                self.args = (
                    received_db,
                    received_session_id,
                    received_message,
                    received_owner_customer_id,
                )
                return {"status": "awaiting_update", "response": "Pilih ID reservasi"}

        handler = DummyUpdateReservationAgent()
        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.update_reservation_agent = handler

        owner_customer_id = "update-owner"
        response = asyncio.run(
            orchestrator.handle(
                "update-new-session",
                "ubah booking saya",
                db,
                owner_customer_id=owner_customer_id,
            )
        )

        self.assertEqual(response, "Pilih ID reservasi")
        self.assertEqual(
            handler.args,
            (db, "update-new-session", "ubah booking saya", owner_customer_id),
        )

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

        response = asyncio.run(
            orchestrator.handle(
                session_id,
                "Ya",
                db,
                owner_customer_id=self.service.OWNER_ID,
            ),
        )

        self.assertEqual(response, "Ditangani confirmation flow")

    def test_classifier_declares_update_reservation_intent(self):
        self.assertIn(
            "update_reservation",
            IntentClassifier._get_supported_intents(IntentClassifier()),
        )


if __name__ == "__main__":
    unittest.main()
