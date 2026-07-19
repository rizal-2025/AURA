import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.orchestrator import AgentOrchestrator
from app.brain.classifier import IntentClassifier
from app.brain.memory_manager import MemoryManager
from app.db.repositories.reservation_repository import ReservationRepository


class FakeCancellationService:
    OWNER_ID = "cancel-session"

    def __init__(self):
        self.reservations = {
            5: SimpleNamespace(
                id=5,
                name="Legacy",
                people=1,
                date="2026-07-23",
                time="17:00",
                status="pending",
                customer_id=None,
            ),
            4: SimpleNamespace(
                id=4,
                name="Customer Lain",
                people=3,
                date="2026-07-23",
                time="18:00",
                status="pending",
                customer_id="other-session",
            ),
            3: SimpleNamespace(
                id=3,
                name="Citra",
                people=5,
                date="2026-07-22",
                time="20:00",
                status="cancelled",
                customer_id=self.OWNER_ID,
            ),
            2: SimpleNamespace(
                id=2,
                name="Rizal",
                people=4,
                date="2026-07-21",
                time="19:00",
                status="pending",
                customer_id=self.OWNER_ID,
            ),
            1: SimpleNamespace(
                id=1,
                name="Budi",
                people=2,
                date="2026-07-20",
                time="18:00",
                status="pending",
                customer_id=self.OWNER_ID,
            ),
        }
        self.list_calls = []
        self.cancel_calls = []

    def list_recent_reservations(self, db, customer_id, limit=5):
        self.list_calls.append((db, customer_id, limit))
        reservations = [
            reservation
            for reservation in self.reservations.values()
            if reservation.customer_id == customer_id
        ]
        return sorted(
            reservations,
            key=lambda reservation: reservation.id,
            reverse=True,
        )[:limit]

    def get_reservation_by_id(self, db, reservation_id, customer_id):
        reservation = self.reservations.get(reservation_id)
        if reservation is None or reservation.customer_id != customer_id:
            return None
        return reservation

    def cancel_reservation(self, db, reservation_id, customer_id):
        reservation = self.get_reservation_by_id(db, reservation_id, customer_id)
        if reservation is None or reservation.status == "cancelled":
            return None

        reservation.status = "cancelled"
        self.cancel_calls.append((db, reservation_id, customer_id))
        return reservation


class FakeCancelQuery:
    def __init__(self, reservation):
        self.reservation = reservation
        self.filter_clauses = []

    def filter(self, clause):
        self.filter_clauses.append(clause)
        return self

    def first(self):
        return self.reservation


class TestCancelReservation(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryManager()
        self.service = FakeCancellationService()
        self.agent = CancelReservationAgent(
            memory_manager=self.memory,
            reservation_service=self.service,
        )
        self.db = MagicMock()
        self.session_id = "cancel-session"

    def _send(self, message):
        return asyncio.run(self.agent.run(self.db, self.session_id, message))

    def _start_and_select_reservation(self, reservation_id="2"):
        self._send("batalkan reservasi saya")
        return self._send(reservation_id)

    def test_successful_cancellation(self):
        start_result = self._send("batalkan reservasi saya")
        selected_result = self._send("2")
        result = self._send("Ya")
        session = self.memory.get_session(self.session_id)

        self.assertEqual(
            self.service.list_calls,
            [(self.db, self.session_id, 5)],
        )
        self.assertIn("Pilih ID reservasi", start_result["response"])
        self.assertIn("ID: 2", selected_result["response"])
        self.assertIn(
            "Yakin ingin membatalkan reservasi ini? Ya / Tidak",
            selected_result["response"],
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.service.reservations[2].status, "cancelled")
        self.assertEqual(
            self.service.cancel_calls,
            [(self.db, 2, self.session_id)],
        )
        self.assertIn("Status: cancelled", result["response"])
        self.assertIsNone(session["cancel_reservation_stage"])
        self.assertIsNone(session["cancel_reservation_id"])

    def test_invalid_reservation_id(self):
        self._send("batalkan reservasi saya")

        result = self._send("999")
        session = self.memory.get_session(self.session_id)

        self.assertIn("ID reservasi tidak ditemukan", result["response"])
        self.assertEqual(
            session["cancel_reservation_stage"],
            CancelReservationAgent.SELECT_RESERVATION_ID,
        )
        self.assertIsNone(session["cancel_reservation_id"])
        self.assertEqual(self.service.cancel_calls, [])

    def test_user_rejects_cancellation(self):
        self._start_and_select_reservation()

        result = self._send("Tidak")
        session = self.memory.get_session(self.session_id)

        self.assertEqual(result["status"], "cancellation_rejected")
        self.assertIn("Tidak ada perubahan", result["response"])
        self.assertEqual(self.service.reservations[2].status, "pending")
        self.assertEqual(self.service.cancel_calls, [])
        self.assertIsNone(session["cancel_reservation_stage"])
        self.assertIsNone(session["cancel_reservation_id"])

    def test_already_cancelled_reservation_is_rejected(self):
        self._send("batalkan reservasi saya")

        result = self._send("3")
        session = self.memory.get_session(self.session_id)

        self.assertIn("sudah dibatalkan", result["response"])
        self.assertEqual(
            session["cancel_reservation_stage"],
            CancelReservationAgent.SELECT_RESERVATION_ID,
        )
        self.assertIsNone(session["cancel_reservation_id"])
        self.assertEqual(self.service.cancel_calls, [])

    def test_state_persists_across_messages(self):
        self._send("batalkan reservasi saya")
        session = self.memory.get_session(self.session_id)
        self.assertEqual(
            session["cancel_reservation_stage"],
            CancelReservationAgent.SELECT_RESERVATION_ID,
        )

        self._send("2")
        self.assertEqual(session["cancel_reservation_id"], 2)
        self.assertEqual(
            session["cancel_reservation_stage"],
            CancelReservationAgent.CONFIRM_CANCELLATION,
        )

        result = self._send("mungkin")
        self.assertEqual(result["status"], "awaiting_cancellation")
        self.assertEqual(session["cancel_reservation_id"], 2)
        self.assertEqual(
            session["cancel_reservation_stage"],
            CancelReservationAgent.CONFIRM_CANCELLATION,
        )

    def test_repository_cancels_by_updating_status_without_deleting(self):
        reservation = SimpleNamespace(
            id=2,
            name="Rizal",
            people=4,
            date="2026-07-21",
            time="19:00",
            status="pending",
            customer_id=self.session_id,
        )
        db = MagicMock()
        db.query.return_value = FakeCancelQuery(reservation)

        updated = ReservationRepository().cancel_reservation(
            db,
            2,
            self.session_id,
        )

        self.assertIs(updated, reservation)
        self.assertEqual(reservation.status, "cancelled")
        db.commit.assert_called_once()
        db.refresh.assert_called_once_with(reservation)
        db.delete.assert_not_called()

    def test_repository_rejects_already_cancelled_reservation(self):
        reservation = SimpleNamespace(
            id=3,
            name="Citra",
            people=5,
            date="2026-07-22",
            time="20:00",
            status="cancelled",
            customer_id=self.session_id,
        )
        db = MagicMock()
        db.query.return_value = FakeCancelQuery(reservation)

        result = ReservationRepository().cancel_reservation(
            db,
            3,
            self.session_id,
        )

        self.assertIsNone(result)
        db.commit.assert_not_called()
        db.refresh.assert_not_called()
        db.delete.assert_not_called()

    def test_user_cannot_cancel_another_customers_reservation(self):
        self._send("batalkan reservasi saya")

        result = self._send("4")
        session = self.memory.get_session(self.session_id)

        self.assertIn("ID reservasi tidak ditemukan", result["response"])
        self.assertEqual(
            session["cancel_reservation_stage"],
            CancelReservationAgent.SELECT_RESERVATION_ID,
        )
        self.assertEqual(self.service.reservations[4].status, "pending")
        self.assertEqual(self.service.cancel_calls, [])

    def test_legacy_reservation_is_not_listed_or_selectable(self):
        start_result = self._send("batalkan reservasi saya")
        result = self._send("5")

        self.assertNotIn("Legacy", start_result["response"])
        self.assertIn("ID reservasi tidak ditemukan", result["response"])
        self.assertEqual(self.service.reservations[5].status, "pending")

    def test_orchestrator_routes_each_cancel_phrase(self):
        phrases = (
            "batalkan reservasi saya",
            "cancel reservasi",
            "saya ingin membatalkan reservasi",
            "cancel my reservation",
        )

        for index, phrase in enumerate(phrases):
            with self.subTest(phrase=phrase):
                orchestrator = AgentOrchestrator()
                session_id = f"cancel-phrase-{index}"
                orchestrator.memory_manager.update_session(
                    session_id,
                    {"intent": "reservation"},
                )

                class DummyCancelReservationAgent:
                    async def run(self, received_db, received_session_id, received_message):
                        self.args = (
                            received_db,
                            received_session_id,
                            received_message,
                        )
                        return {
                            "status": "awaiting_cancellation",
                            "response": "Pilih ID reservasi",
                        }

                handler = DummyCancelReservationAgent()
                orchestrator.cancel_reservation_agent = handler
                db = MagicMock()

                response = asyncio.run(orchestrator.handle(session_id, phrase, db))

                self.assertEqual(response, "Pilih ID reservasi")
                self.assertEqual(handler.args, (db, session_id, phrase))
                self.assertEqual(
                    orchestrator.memory_manager.get_session(session_id)["intent"],
                    "reservation",
                )

    def test_orchestrator_routes_classified_cancel_intent(self):
        orchestrator = AgentOrchestrator()
        db = MagicMock()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "cancel_reservation", "confidence": 0.95}

        class DummyCancelReservationAgent:
            async def run(self, received_db, received_session_id, received_message):
                self.args = (received_db, received_session_id, received_message)
                return {
                    "status": "awaiting_cancellation",
                    "response": "Pilih ID reservasi",
                }

        handler = DummyCancelReservationAgent()
        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.cancel_reservation_agent = handler

        response = asyncio.run(
            orchestrator.handle("cancel-new-session", "hapus booking saya", db)
        )

        self.assertEqual(response, "Pilih ID reservasi")
        self.assertEqual(
            handler.args,
            (db, "cancel-new-session", "hapus booking saya"),
        )

    def test_orchestrator_keeps_create_confirmation_priority_over_cancel_state(self):
        orchestrator = AgentOrchestrator()
        db = MagicMock()
        session_id = "cancel-confirmation-priority"
        session = orchestrator.memory_manager.get_session(session_id)
        session.update(
            {
                "intent": "reservation",
                "awaiting_confirmation": True,
                "cancel_reservation_id": 2,
                "cancel_reservation_stage": CancelReservationAgent.CONFIRM_CANCELLATION,
            }
        )

        class DummyWorkflow:
            async def execute(self, *args, **kwargs):
                return {
                    "status": "awaiting_confirmation",
                    "response": "Ditangani confirmation flow",
                }

        class FailingCancelReservationAgent:
            async def run(self, *args, **kwargs):
                raise AssertionError("Cancel flow must not replace confirmation flow")

        orchestrator.workflow = DummyWorkflow()
        orchestrator.cancel_reservation_agent = FailingCancelReservationAgent()

        response = asyncio.run(orchestrator.handle(session_id, "Ya", db))

        self.assertEqual(response, "Ditangani confirmation flow")

    def test_classifier_declares_cancel_reservation_intent(self):
        self.assertIn(
            "cancel_reservation",
            IntentClassifier._get_supported_intents(IntentClassifier()),
        )


if __name__ == "__main__":
    unittest.main()
