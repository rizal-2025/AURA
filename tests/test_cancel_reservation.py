import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
                owner_customer_id=None,
            ),
            4: SimpleNamespace(
                id=4,
                name="Customer Lain",
                people=3,
                date="2026-07-23",
                time="18:00",
                status="pending",
                owner_customer_id="other-customer",
            ),
            3: SimpleNamespace(
                id=3,
                name="Citra",
                people=5,
                date="2026-07-22",
                time="20:00",
                status="cancelled",
                owner_customer_id=self.OWNER_ID,
            ),
            2: SimpleNamespace(
                id=2,
                name="Rizal",
                people=4,
                date="2026-07-21",
                time="19:00",
                status="pending",
                owner_customer_id=self.OWNER_ID,
            ),
            1: SimpleNamespace(
                id=1,
                name="Budi",
                people=2,
                date="2026-07-20",
                time="18:00",
                status="pending",
                owner_customer_id=self.OWNER_ID,
            ),
        }
        self.list_calls = []
        self.cancel_calls = []

    def list_recent_reservations(self, db, owner_customer_id, limit=5):
        self.list_calls.append((db, owner_customer_id, limit))
        reservations = [
            reservation
            for reservation in self.reservations.values()
            if reservation.owner_customer_id == owner_customer_id
        ]
        return sorted(
            reservations,
            key=lambda reservation: reservation.id,
            reverse=True,
        )[:limit]

    def get_reservation_by_id(self, db, reservation_id, owner_customer_id):
        reservation = self.reservations.get(reservation_id)
        if reservation is None or reservation.owner_customer_id != owner_customer_id:
            return None
        return reservation

    def cancel_reservation(self, db, reservation_id, owner_customer_id):
        reservation = self.get_reservation_by_id(db, reservation_id, owner_customer_id)
        if reservation is None or reservation.status == "cancelled":
            return None

        reservation.status = "cancelled"
        self.cancel_calls.append((db, reservation_id, owner_customer_id))
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


class FakeAtomicResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


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
        return asyncio.run(
            self.agent.run(
                self.db,
                self.session_id,
                message,
                self.service.OWNER_ID,
            )
        )

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
            [(self.db, self.service.OWNER_ID, 5)],
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
            [(self.db, 2, self.service.OWNER_ID)],
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
            owner_customer_id=self.service.OWNER_ID,
        )
        db = MagicMock()
        db.execute.return_value = FakeAtomicResult(rowcount=1)
        repository = ReservationRepository()

        with patch.object(
            repository,
            "get_by_id",
            return_value=reservation,
        ) as get_by_id:
            updated = repository.cancel_reservation(
                db,
                2,
                self.service.OWNER_ID,
            )

        self.assertIs(updated, reservation)
        db.commit.assert_called_once()
        get_by_id.assert_called_once_with(db, 2, self.service.OWNER_ID)
        statement = db.execute.call_args.args[0]
        self.assertIn("reservations.id", str(statement))
        self.assertIn("owner_customer_id", str(statement))
        self.assertIn("cancelled", statement.compile().params.values())
        db.delete.assert_not_called()

    def test_repository_atomic_cancel_rejects_non_owner_or_cancelled_record(self):
        db = MagicMock()
        db.execute.return_value = FakeAtomicResult(rowcount=0)
        repository = ReservationRepository()

        result = repository.cancel_reservation(
            db,
            3,
            self.service.OWNER_ID,
        )

        self.assertIsNone(result)
        db.commit.assert_not_called()
        statement = db.execute.call_args.args[0]
        self.assertIn("reservations.id", str(statement))
        self.assertIn("owner_customer_id", str(statement))
        self.assertIn("cancelled", statement.compile().params.values())
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
                        return {
                            "status": "awaiting_cancellation",
                            "response": "Pilih ID reservasi",
                        }

                handler = DummyCancelReservationAgent()
                orchestrator.cancel_reservation_agent = handler
                db = MagicMock()

                owner_customer_id = f"cancel-owner-{index}"
                response = asyncio.run(
                    orchestrator.handle(
                        session_id,
                        phrase,
                        db,
                        owner_customer_id=owner_customer_id,
                    )
                )

                self.assertEqual(response, "Pilih ID reservasi")
                self.assertEqual(
                    handler.args,
                    (db, session_id, phrase, owner_customer_id),
                )
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
                return {
                    "status": "awaiting_cancellation",
                    "response": "Pilih ID reservasi",
                }

        handler = DummyCancelReservationAgent()
        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.cancel_reservation_agent = handler

        owner_customer_id = "cancel-owner"
        response = asyncio.run(
            orchestrator.handle(
                "cancel-new-session",
                "hapus booking saya",
                db,
                owner_customer_id=owner_customer_id,
            )
        )

        self.assertEqual(response, "Pilih ID reservasi")
        self.assertEqual(
            handler.args,
            (db, "cancel-new-session", "hapus booking saya", owner_customer_id),
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
