import unittest
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.chat import agent as chat_agent
from app.core.config import settings
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.security import create_customer_access_token
from app.db.database import get_db
from app.main import app


class FakeCustomerDB:
    def __init__(self, customers):
        self.customers = customers

    def get(self, _model, customer_id):
        return self.customers.get(customer_id)

    def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


class InMemorySecureReservationService:
    def __init__(self, customer_a_id, customer_b_id):
        self.reservations = {
            3: SimpleNamespace(
                id=3,
                name="Legacy",
                people=1,
                date="2026-08-03",
                time="18:00",
                status="pending",
                owner_customer_id=None,
            ),
            2: SimpleNamespace(
                id=2,
                name="Customer B",
                people=3,
                date="2026-08-02",
                time="19:00",
                status="pending",
                owner_customer_id=customer_b_id,
            ),
            1: SimpleNamespace(
                id=1,
                name="Customer A",
                people=4,
                date="2026-08-01",
                time="20:00",
                status="pending",
                owner_customer_id=customer_a_id,
            ),
        }
        self.list_owners = []
        self.update_calls = []
        self.cancel_calls = []

    def list_recent_reservations(self, _db, owner_customer_id, limit=5):
        self.list_owners.append(owner_customer_id)
        return sorted(
            (
                reservation
                for reservation in self.reservations.values()
                if reservation.owner_customer_id == owner_customer_id
            ),
            key=lambda reservation: reservation.id,
            reverse=True,
        )[:limit]

    def get_reservation_by_id(self, _db, reservation_id, owner_customer_id):
        reservation = self.reservations.get(reservation_id)
        if reservation is None or reservation.owner_customer_id != owner_customer_id:
            return None
        return reservation

    def update_reservation_field(
        self,
        _db,
        reservation_id,
        field_name,
        new_value,
        owner_customer_id,
    ):
        reservation = self.get_reservation_by_id(
            _db,
            reservation_id,
            owner_customer_id,
        )
        if reservation is None:
            return None
        setattr(reservation, field_name, new_value)
        self.update_calls.append((reservation_id, owner_customer_id))
        return reservation

    def cancel_reservation(self, _db, reservation_id, owner_customer_id):
        reservation = self.get_reservation_by_id(
            _db,
            reservation_id,
            owner_customer_id,
        )
        if reservation is None or reservation.status == "cancelled":
            return None
        reservation.status = "cancelled"
        self.cancel_calls.append((reservation_id, owner_customer_id))
        return reservation


class TestSecureReservationManagement(unittest.TestCase):
    def setUp(self):
        self.original_secret = settings.AUTH_JWT_SECRET
        self.original_issuer = settings.AUTH_JWT_ISSUER
        self.original_audience = settings.AUTH_JWT_AUDIENCE
        settings.AUTH_JWT_SECRET = "phase-2b-test-secret-01234567890"
        settings.AUTH_JWT_ISSUER = "aura-phase-2b"
        settings.AUTH_JWT_AUDIENCE = "aura-phase-2b-api"

        self.customer_a = SimpleNamespace(id=uuid4(), is_active=True, token_version=1)
        self.customer_b = SimpleNamespace(id=uuid4(), is_active=True, token_version=1)
        self.db = FakeCustomerDB(
            {
                self.customer_a.id: self.customer_a,
                self.customer_b.id: self.customer_b,
            }
        )
        self.service = InMemorySecureReservationService(
            self.customer_a.id,
            self.customer_b.id,
        )
        self.original_view_service = chat_agent.view_reservation_agent.reservation_service
        self.original_update_service = chat_agent.update_reservation_agent.reservation_service
        self.original_cancel_service = chat_agent.cancel_reservation_agent.reservation_service
        chat_agent.view_reservation_agent.reservation_service = self.service
        chat_agent.update_reservation_agent.reservation_service = self.service
        chat_agent.cancel_reservation_agent.reservation_service = self.service

        def override_get_db():
            yield self.db

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        chat_agent.view_reservation_agent.reservation_service = self.original_view_service
        chat_agent.update_reservation_agent.reservation_service = self.original_update_service
        chat_agent.cancel_reservation_agent.reservation_service = self.original_cancel_service
        for session_id in (
            "read-a",
            "read-b",
            "update-b",
            "cancel-b",
            "shared-update",
            "separate-update",
            "shared-cancel",
        ):
            for customer in (self.customer_a, self.customer_b):
                chat_agent.memory_manager.clear_session(
                    self._memory_key(customer, session_id),
                )
        settings.AUTH_JWT_SECRET = self.original_secret
        settings.AUTH_JWT_ISSUER = self.original_issuer
        settings.AUTH_JWT_AUDIENCE = self.original_audience

    def _headers(self, customer):
        token, _ = create_customer_access_token(customer.id, customer.token_version)
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _memory_key(customer, session_id):
        return build_authenticated_memory_key(customer.id, session_id)

    def _chat(self, customer, session_id, message):
        return self.client.post(
            "/chat",
            json={"session_id": session_id, "message": message},
            headers=self._headers(customer),
        )

    def test_customers_only_see_their_own_non_legacy_reservations(self):
        response_a = self._chat(self.customer_a, "read-a", "lihat reservasi saya")
        response_b = self._chat(self.customer_b, "read-b", "reservasi saya")

        self.assertEqual(response_a.status_code, 200)
        self.assertIn("Customer A", response_a.json()["reply"])
        self.assertNotIn("Customer B", response_a.json()["reply"])
        self.assertNotIn("Legacy", response_a.json()["reply"])

        self.assertEqual(response_b.status_code, 200)
        self.assertIn("Customer B", response_b.json()["reply"])
        self.assertNotIn("Customer A", response_b.json()["reply"])
        self.assertNotIn("Legacy", response_b.json()["reply"])
        self.assertEqual(
            self.service.list_owners,
            [self.customer_a.id, self.customer_b.id],
        )

    def test_customer_b_cannot_update_or_cancel_customer_a_reservation(self):
        update_start = self._chat(self.customer_b, "update-b", "ubah reservasi saya")
        update_attempt = self._chat(self.customer_b, "update-b", "1")

        cancel_start = self._chat(
            self.customer_b,
            "cancel-b",
            "batalkan reservasi saya",
        )
        cancel_attempt = self._chat(self.customer_b, "cancel-b", "1")

        self.assertEqual(update_start.status_code, 200)
        self.assertIn("Customer B", update_start.json()["reply"])
        self.assertIn("ID reservasi tidak ditemukan", update_attempt.json()["reply"])
        self.assertEqual(cancel_start.status_code, 200)
        self.assertIn("Customer B", cancel_start.json()["reply"])
        self.assertIn("ID reservasi tidak ditemukan", cancel_attempt.json()["reply"])
        self.assertEqual(self.service.reservations[1].people, 4)
        self.assertEqual(self.service.reservations[1].status, "pending")
        self.assertEqual(self.service.update_calls, [])
        self.assertEqual(self.service.cancel_calls, [])

    def test_memory_is_scoped_by_authenticated_customer_and_session_id(self):
        shared_session_id = "shared-update"

        self._chat(self.customer_a, shared_session_id, "ubah reservasi saya")
        select_a = self._chat(self.customer_a, shared_session_id, "1")
        self._chat(self.customer_b, shared_session_id, "ubah reservasi saya")
        select_b = self._chat(self.customer_b, shared_session_id, "2")

        state_a = chat_agent.memory_manager.get_session(
            self._memory_key(self.customer_a, shared_session_id),
        )
        state_b = chat_agent.memory_manager.get_session(
            self._memory_key(self.customer_b, shared_session_id),
        )

        self.assertEqual(select_a.status_code, 200)
        self.assertIn("field mana", select_a.json()["reply"].lower())
        self.assertEqual(select_b.status_code, 200)
        self.assertIn("field mana", select_b.json()["reply"].lower())
        self.assertEqual(state_a["reservation_id"], 1)
        self.assertEqual(state_b["reservation_id"], 2)
        self.assertNotIn(shared_session_id, chat_agent.memory_manager._sessions)

    def test_same_customer_different_session_ids_have_separate_update_state(self):
        self._chat(self.customer_a, "shared-update", "ubah reservasi saya")
        self._chat(self.customer_a, "shared-update", "1")
        self._chat(self.customer_a, "separate-update", "ubah reservasi saya")

        first_state = chat_agent.memory_manager.get_session(
            self._memory_key(self.customer_a, "shared-update"),
        )
        second_state = chat_agent.memory_manager.get_session(
            self._memory_key(self.customer_a, "separate-update"),
        )

        self.assertEqual(first_state["reservation_id"], 1)
        self.assertIsNone(second_state.get("reservation_id"))
        self.assertNotEqual(
            first_state["update_reservation_stage"],
            second_state["update_reservation_stage"],
        )

    def test_cancel_state_is_scoped_by_authenticated_customer_and_session_id(self):
        shared_session_id = "shared-cancel"
        self._chat(self.customer_a, shared_session_id, "batalkan reservasi saya")
        self._chat(self.customer_b, shared_session_id, "batalkan reservasi saya")

        state_a = chat_agent.memory_manager.get_session(
            self._memory_key(self.customer_a, shared_session_id),
        )
        state_b = chat_agent.memory_manager.get_session(
            self._memory_key(self.customer_b, shared_session_id),
        )

        self.assertEqual(state_a["cancel_reservation_stage"], "select_reservation_id")
        self.assertEqual(state_b["cancel_reservation_stage"], "select_reservation_id")
        self.assertNotEqual(
            self._memory_key(self.customer_a, shared_session_id),
            self._memory_key(self.customer_b, shared_session_id),
        )


if __name__ == "__main__":
    unittest.main()
