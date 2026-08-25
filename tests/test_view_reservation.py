import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.agents.orchestrator import AgentOrchestrator
from app.agents.view_reservation_agent import ViewReservationAgent
from app.brain.classifier import IntentClassifier
from app.db.models.reservation import Reservation
from app.db.repositories.reservation_repository import ReservationRepository
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
)


SEEDED_VIEW_RESERVATION_IDS = (
    (2**30) + 104_759,
    (2**30) + 104_761,
)


def reference_for(index: int) -> str:
    return f"RSV_{index:032x}"


class FakeQuery:
    def __init__(self, results):
        self.results = results
        self.filter_clauses = []
        self.order_by_clause = None
        self.limit_value = None

    def filter(self, clause):
        self.filter_clauses.append(clause)
        return self

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

        result = ReservationRepository().list_recent(
            db,
            owner_customer_id=uuid4(),
        )

        db.query.assert_called_once_with(Reservation)
        self.assertEqual(len(query.filter_clauses), 1)
        self.assertEqual(str(query.order_by_clause), str(Reservation.id.desc()))
        self.assertEqual(query.limit_value, 5)
        self.assertEqual(result, ["latest"])

    def test_view_agent_formats_only_five_latest_reservations(self):
        reservations = [
            SimpleNamespace(
                id=reservation_id,
                reference=reference_for(reservation_id),
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

        owner_customer_id = uuid4()
        result = asyncio.run(agent.run(db, "memory-session", owner_customer_id))

        service.list_recent_reservations.assert_called_once_with(
            db,
            owner_customer_id=owner_customer_id,
            limit=5,
        )
        self.assertEqual(result["status"], "viewed")
        self.assertIn(reference_for(6), result["response"])
        self.assertIn(reference_for(2), result["response"])
        self.assertNotIn(reference_for(1), result["response"])
        self.assertNotIn("ID:", result["response"])
        self.assertLess(
            result["response"].index(reference_for(6)),
            result["response"].index(reference_for(2)),
        )
        self.assertIn("Nama: User 6", result["response"])
        self.assertIn("Jumlah orang: 6 orang", result["response"])
        self.assertIn("Status: Menunggu", result["response"])
        self.assertIn("Tanggal: 20 Juli 2026", result["response"])
        self.assertIn("Waktu: 19.00", result["response"])

    def test_view_reply_omits_exact_seeded_ids_for_distinct_references(self):
        reservations = [
            SimpleNamespace(
                id=reservation_id,
                reference=reference_for(reservation_id),
                name="Nama Sama",
                people=4,
                date="2026-07-20",
                time="19:00",
                status="pending",
            )
            for reservation_id in reversed(SEEDED_VIEW_RESERVATION_IDS)
        ]
        service = MagicMock()
        service.list_recent_reservations.return_value = reservations

        result = asyncio.run(
            ViewReservationAgent(reservation_service=service).run(
                MagicMock(),
                "seeded-view-session",
                uuid4(),
            )
        )

        for reservation_id in SEEDED_VIEW_RESERVATION_IDS:
            self.assertNotIn(str(reservation_id), result["response"])
            self.assertIn(reference_for(reservation_id), result["response"])
        self.assertLess(
            result["response"].index(reference_for(SEEDED_VIEW_RESERVATION_IDS[1])),
            result["response"].index(reference_for(SEEDED_VIEW_RESERVATION_IDS[0])),
        )

    def test_view_agent_returns_empty_message_when_no_reservations_exist(self):
        service = MagicMock()
        service.list_recent_reservations.return_value = []
        agent = ViewReservationAgent(reservation_service=service)

        result = asyncio.run(agent.run(MagicMock(), "memory-session", uuid4()))

        self.assertEqual(result, {"status": "viewed", "response": "Belum ada reservasi."})

    def test_view_agent_fails_whole_response_for_unsafe_stored_reference(self):
        service = MagicMock()
        service.list_recent_reservations.side_effect = (
            PublicReservationReferenceUnavailableError()
        )
        result = asyncio.run(
            ViewReservationAgent(reservation_service=service).run(
                MagicMock(),
                "memory-session",
                uuid4(),
            )
        )

        self.assertEqual(result["status"], "reference_unavailable")
        self.assertEqual(
            result["response"],
            "Data reservasi belum dapat ditampilkan dengan aman. Silakan coba lagi nanti.",
        )

    def test_orchestrator_routes_view_request_without_changing_active_reservation_intent(self):
        orchestrator = AgentOrchestrator()
        session_id = "view-existing-session"
        db = MagicMock()
        orchestrator.memory_manager.update_session(session_id, {"intent": "reservation"})

        class DummyViewReservationAgent:
            def __init__(self):
                self.db = None

            async def run(self, received_db, received_session_id, received_owner_customer_id):
                self.db = received_db
                self.session_id = received_session_id
                self.owner_customer_id = received_owner_customer_id
                return {"status": "viewed", "response": "Daftar reservasi terbaru"}

        handler = DummyViewReservationAgent()
        orchestrator.view_reservation_agent = handler

        response = asyncio.run(
            orchestrator.handle(
                session_id,
                "lihat reservasi saya",
                db,
                owner_customer_id=uuid4(),
            )
        )

        self.assertEqual(response, "Daftar reservasi terbaru")
        self.assertIs(handler.db, db)
        self.assertEqual(handler.session_id, session_id)
        self.assertEqual(orchestrator.memory_manager.get_session(session_id)["intent"], "reservation")

    def test_orchestrator_routes_classified_view_intent_for_a_new_session(self):
        orchestrator = AgentOrchestrator()
        db = MagicMock()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "view_reservation", "confidence": 0.95}

        class DummyViewReservationAgent:
            async def run(self, received_db, received_session_id, received_owner_customer_id):
                self.db = received_db
                self.session_id = received_session_id
                self.owner_customer_id = received_owner_customer_id
                return {"status": "viewed", "response": "Daftar reservasi terbaru"}

        handler = DummyViewReservationAgent()
        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.view_reservation_agent = handler

        owner_customer_id = uuid4()
        response = asyncio.run(
            orchestrator.handle(
                "view-new-session",
                "tampilkan riwayat booking",
                db,
                owner_customer_id=owner_customer_id,
            )
        )

        self.assertEqual(response, "Daftar reservasi terbaru")
        self.assertIs(handler.db, db)
        self.assertEqual(handler.session_id, "view-new-session")

    def test_view_only_returns_current_customer_records_and_hides_legacy(self):
        reservations = [
            SimpleNamespace(
                id=9,
                reference=reference_for(9),
                name="Customer B",
                people=2,
                date="2026-07-20",
                time="19:00",
                status="pending",
                owner_customer_id="customer-b",
            ),
            SimpleNamespace(
                id=8,
                reference=reference_for(8),
                name="Legacy",
                people=3,
                date="2026-07-20",
                time="19:00",
                status="pending",
                owner_customer_id=None,
            ),
            SimpleNamespace(
                id=7,
                reference=reference_for(7),
                name="Customer A 2",
                people=4,
                date="2026-07-20",
                time="19:00",
                status="pending",
                owner_customer_id="customer-a",
            ),
            SimpleNamespace(
                id=6,
                reference=reference_for(6),
                name="Customer A 1",
                people=5,
                date="2026-07-20",
                time="19:00",
                status="pending",
                owner_customer_id="customer-a",
            ),
        ]

        class OwnedReservationService:
            def list_recent_reservations(self, db, owner_customer_id, limit=5):
                return [
                    reservation
                    for reservation in reservations
                    if reservation.owner_customer_id == owner_customer_id
                ][:limit]

        result = asyncio.run(
            ViewReservationAgent(
                reservation_service=OwnedReservationService(),
            ).run(MagicMock(), "memory-session", "customer-a")
        )

        self.assertIn(reference_for(7), result["response"])
        self.assertIn(reference_for(6), result["response"])
        self.assertNotIn("Customer B", result["response"])
        self.assertNotIn("Legacy", result["response"])

    def test_classifier_declares_view_reservation_intent(self):
        self.assertIn("view_reservation", IntentClassifier._get_supported_intents(IntentClassifier()))


if __name__ == "__main__":
    unittest.main()
