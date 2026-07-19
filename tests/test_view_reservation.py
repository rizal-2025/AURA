import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agents.orchestrator import AgentOrchestrator
from app.agents.view_reservation_agent import ViewReservationAgent
from app.brain.classifier import IntentClassifier
from app.db.models.reservation import Reservation
from app.db.repositories.reservation_repository import ReservationRepository


class FakeQuery:
    def __init__(self, results):
        self.results = results
        self.order_by_clause = None
        self.limit_value = None

    def order_by(self, clause):
        self.order_by_clause = clause
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def all(self):
        return self.results


class TestViewReservation(unittest.TestCase):
    def test_repository_lists_latest_five_by_descending_id(self):
        query = FakeQuery(results=["latest"])
        db = MagicMock()
        db.query.return_value = query

        result = ReservationRepository().list_recent(db)

        db.query.assert_called_once_with(Reservation)
        self.assertEqual(str(query.order_by_clause), str(Reservation.id.desc()))
        self.assertEqual(query.limit_value, 5)
        self.assertEqual(result, ["latest"])

    def test_view_agent_formats_only_five_latest_reservations(self):
        reservations = [
            SimpleNamespace(
                id=reservation_id,
                name=f"User {reservation_id}",
                people=reservation_id,
                date="2026-07-20",
                time="19:00",
                status="pending",
            )
            for reservation_id in (6, 5, 4, 3, 2, 1)
        ]
        service = MagicMock()
        service.list_recent_reservations.return_value = reservations
        agent = ViewReservationAgent(reservation_service=service)
        db = MagicMock()

        result = asyncio.run(agent.run(db))

        service.list_recent_reservations.assert_called_once_with(db, limit=5)
        self.assertEqual(result["status"], "viewed")
        self.assertIn("ID: 6", result["response"])
        self.assertIn("ID: 2", result["response"])
        self.assertNotIn("ID: 1", result["response"])
        self.assertLess(result["response"].index("ID: 6"), result["response"].index("ID: 2"))
        self.assertIn("Nama: User 6", result["response"])
        self.assertIn("Jumlah Orang: 6", result["response"])
        self.assertIn("Tanggal: 2026-07-20", result["response"])
        self.assertIn("Jam: 19:00", result["response"])
        self.assertIn("Status: pending", result["response"])

    def test_view_agent_returns_empty_message_when_no_reservations_exist(self):
        service = MagicMock()
        service.list_recent_reservations.return_value = []
        agent = ViewReservationAgent(reservation_service=service)

        result = asyncio.run(agent.run(MagicMock()))

        self.assertEqual(result, {"status": "viewed", "response": "Belum ada reservasi."})

    def test_orchestrator_routes_view_request_without_changing_active_reservation_intent(self):
        orchestrator = AgentOrchestrator()
        session_id = "view-existing-session"
        db = MagicMock()
        orchestrator.memory_manager.update_session(session_id, {"intent": "reservation"})

        class DummyViewReservationAgent:
            def __init__(self):
                self.db = None

            async def run(self, received_db):
                self.db = received_db
                return {"status": "viewed", "response": "Daftar reservasi terbaru"}

        handler = DummyViewReservationAgent()
        orchestrator.view_reservation_agent = handler

        response = asyncio.run(
            orchestrator.handle(session_id, "lihat reservasi saya", db)
        )

        self.assertEqual(response, "Daftar reservasi terbaru")
        self.assertIs(handler.db, db)
        self.assertEqual(orchestrator.memory_manager.get_session(session_id)["intent"], "reservation")

    def test_orchestrator_routes_classified_view_intent_for_a_new_session(self):
        orchestrator = AgentOrchestrator()
        db = MagicMock()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "view_reservation", "confidence": 0.95}

        class DummyViewReservationAgent:
            async def run(self, received_db):
                self.db = received_db
                return {"status": "viewed", "response": "Daftar reservasi terbaru"}

        handler = DummyViewReservationAgent()
        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.view_reservation_agent = handler

        response = asyncio.run(
            orchestrator.handle("view-new-session", "tampilkan riwayat booking", db)
        )

        self.assertEqual(response, "Daftar reservasi terbaru")
        self.assertIs(handler.db, db)

    def test_classifier_declares_view_reservation_intent(self):
        self.assertIn("view_reservation", IntentClassifier._get_supported_intents(IntentClassifier()))


if __name__ == "__main__":
    unittest.main()
