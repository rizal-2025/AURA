import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.agents.update_reservation_agent import UpdateReservationAgent
from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.view_reservation_agent import ViewReservationAgent
from app.api.reservation import create as create_reservation_endpoint
from app.brain.memory_manager import MemoryManager
from app.core.ownership import MissingOwnerCustomerError
from app.db.models.reservation import Reservation
from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.reservation import ReservationCreate
from app.services.reservation.service import ReservationService


class InMemoryOwnedReservationService:
    def __init__(self):
        self.reservations = {
            4: SimpleNamespace(
                id=4,
                name="Legacy",
                people=1,
                date="2026-07-24",
                time="18:00",
                status="pending",
                owner_customer_id=None,
            ),
            3: SimpleNamespace(
                id=3,
                name="Budi",
                people=2,
                date="2026-07-23",
                time="19:00",
                status="pending",
                owner_customer_id="customer-b",
            ),
            2: SimpleNamespace(
                id=2,
                name="Rizal",
                people=4,
                date="2026-07-22",
                time="20:00",
                status="pending",
                owner_customer_id="customer-a",
            ),
        }
        self.update_calls = []

    def list_recent_reservations(self, db, owner_customer_id, limit=5):
        return sorted(
            (
                reservation
                for reservation in self.reservations.values()
                if reservation.owner_customer_id == owner_customer_id
            ),
            key=lambda reservation: reservation.id,
            reverse=True,
        )[:limit]

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
        self.update_calls.append((reservation_id, owner_customer_id))
        return reservation


class TestReservationOwnership(unittest.TestCase):
    def test_customer_id_model_column_is_nullable(self):
        self.assertTrue(Reservation.__table__.c.customer_id.nullable)

    def test_repository_blocks_legacy_only_create(self):
        data = ReservationCreate(
            name="Rizal",
            people=4,
            date="2026-07-22",
            time="19:00",
        )
        db = MagicMock()
        with self.assertRaises(MissingOwnerCustomerError):
            ReservationRepository().create(db, data, owner_customer_id=None)

        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_repository_rejects_new_reservation_without_authenticated_owner(self):
        data = ReservationCreate(
            name="Rizal",
            people=4,
            date="2026-07-22",
            time="19:00",
        )
        db = MagicMock()

        with self.assertRaises(MissingOwnerCustomerError):
            ReservationRepository().create(db, data, owner_customer_id=None)

        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_secure_repository_operations_reject_missing_owner_before_query(self):
        repository = ReservationRepository()
        db = MagicMock()
        data = ReservationCreate(
            name="Rizal",
            people=4,
            date="2026-07-22",
            time="19:00",
        )

        for operation in (
            lambda: repository.list_recent(db, owner_customer_id=None),
            lambda: repository.get_by_id(db, 4, owner_customer_id=None),
            lambda: repository.update_reservation_field(
                db,
                4,
                "people",
                5,
                owner_customer_id=None,
            ),
            lambda: repository.cancel_reservation(db, 4, owner_customer_id=None),
            lambda: repository.create(db, data, owner_customer_id=None),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(MissingOwnerCustomerError):
                    operation()

        db.query.assert_not_called()
        db.execute.assert_not_called()
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_secure_service_operations_reject_missing_owner_before_repository_call(self):
        service = ReservationService()
        service.repository = MagicMock()
        data = ReservationCreate(
            name="Rizal",
            people=4,
            date="2026-07-22",
            time="19:00",
        )
        db = MagicMock()

        for operation in (
            lambda: service.list_recent_reservations(db, owner_customer_id=None),
            lambda: service.get_reservation_by_id(db, 4, owner_customer_id=None),
            lambda: service.update_reservation_field(
                db, 4, "people", 5, owner_customer_id=None
            ),
            lambda: service.cancel_reservation(db, 4, owner_customer_id=None),
            lambda: service.create_reservation(db, data, owner_customer_id=None),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(MissingOwnerCustomerError):
                    operation()

        self.assertEqual(service.repository.method_calls, [])

    def test_management_agents_reject_missing_owner_before_service_call(self):
        service = MagicMock()
        memory = MemoryManager()
        view_result = asyncio.run(
            ViewReservationAgent(reservation_service=service).run(
                MagicMock(), "session-a", None
            )
        )
        update_result = asyncio.run(
            UpdateReservationAgent(memory_manager=memory, reservation_service=service).run(
                MagicMock(), "session-a", "lihat", None
            )
        )
        cancel_result = asyncio.run(
            CancelReservationAgent(memory_manager=memory, reservation_service=service).run(
                MagicMock(), "session-a", "lihat", None
            )
        )

        for result in (view_result, update_result, cancel_result):
            self.assertEqual(result["status"], "authorization_required")

        self.assertEqual(service.method_calls, [])

    def test_repository_create_sets_secure_owner_without_legacy_customer_id(self):
        data = ReservationCreate(
            name="Rizal",
            people=4,
            date="2026-07-22",
            time="19:00",
        )
        owner_customer_id = uuid4()
        db = MagicMock()
        created_reservation = MagicMock()

        with patch(
            "app.db.repositories.reservation_repository.Reservation",
            return_value=created_reservation,
        ) as reservation_model:
            ReservationRepository().create(
                db,
                data,
                owner_customer_id=owner_customer_id,
            )

        reservation_model.assert_called_once_with(
            name="Rizal",
            people=4,
            date="2026-07-22",
            time="19:00",
            owner_customer_id=owner_customer_id,
        )

    def test_direct_create_uses_authenticated_owner_identity(self):
        data = ReservationCreate(
            name="Rizal",
            people=4,
            date="2026-07-22",
            time="19:00",
        )
        db = MagicMock()
        created_reservation = MagicMock()
        owner_customer_id = uuid4()

        with patch(
            "app.api.reservation.service.create_reservation",
            return_value=created_reservation,
        ) as create_reservation:
            result = create_reservation_endpoint(
                data,
                db=db,
                current_customer=SimpleNamespace(id=owner_customer_id),
            )

        self.assertIs(result, created_reservation)
        create_reservation.assert_called_once_with(
            db,
            data,
            owner_customer_id=owner_customer_id,
        )

    def test_two_session_ids_remain_isolated(self):
        memory = MemoryManager()
        service = InMemoryOwnedReservationService()
        agent = UpdateReservationAgent(
            memory_manager=memory,
            reservation_service=service,
        )
        db = MagicMock()

        customer_a_list = asyncio.run(
            agent.run(db, "session-a", "ubah reservasi saya", "customer-a")
        )
        customer_b_list = asyncio.run(
            agent.run(db, "session-b", "ubah reservasi saya", "customer-b")
        )

        self.assertIn("Rizal", customer_a_list["response"])
        self.assertNotIn("Budi", customer_a_list["response"])
        self.assertNotIn("Legacy", customer_a_list["response"])
        self.assertIn("Budi", customer_b_list["response"])
        self.assertNotIn("Rizal", customer_b_list["response"])

        selected = asyncio.run(agent.run(db, "session-a", "2", "customer-a"))
        foreign_selection = asyncio.run(agent.run(db, "session-b", "2", "customer-b"))

        self.assertIn("Reservasi dipilih", selected["response"])
        self.assertIn("ID reservasi tidak ditemukan", foreign_selection["response"])
        self.assertEqual(memory.get_session("session-a")["reservation_id"], 2)
        self.assertIsNone(memory.get_session("session-b").get("reservation_id"))

    def test_legacy_null_record_is_not_exposed_to_view(self):
        service = InMemoryOwnedReservationService()
        agent = ViewReservationAgent(reservation_service=service)

        result = asyncio.run(agent.run(MagicMock(), "session-a", "customer-a"))

        self.assertIn("Rizal", result["response"])
        self.assertNotIn("Legacy", result["response"])


if __name__ == "__main__":
    unittest.main()
