import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient

from app.api.chat import agent as chat_agent
from app.core.config import settings
from app.core.security import JWT_ALGORITHM, create_customer_access_token
from app.db.database import get_db
from app.main import app


class FakeCustomerDB:
    def __init__(self, customers):
        self.customers = customers

    def get(self, _model, customer_id):
        return self.customers.get(customer_id)


class TestSecureReservationCreate(unittest.TestCase):
    def setUp(self):
        self.original_secret = settings.AUTH_JWT_SECRET
        self.original_issuer = settings.AUTH_JWT_ISSUER
        self.original_audience = settings.AUTH_JWT_AUDIENCE
        settings.AUTH_JWT_SECRET = "phase-2a-test-secret"
        settings.AUTH_JWT_ISSUER = "aura-phase-2a"
        settings.AUTH_JWT_AUDIENCE = "aura-phase-2a-api"

        self.customer_a = SimpleNamespace(
            id=uuid4(),
            is_active=True,
            token_version=1,
        )
        self.customer_b = SimpleNamespace(
            id=uuid4(),
            is_active=True,
            token_version=1,
        )
        self.db = FakeCustomerDB(
            {
                self.customer_a.id: self.customer_a,
                self.customer_b.id: self.customer_b,
            }
        )

        def override_get_db():
            yield self.db

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        settings.AUTH_JWT_SECRET = self.original_secret
        settings.AUTH_JWT_ISSUER = self.original_issuer
        settings.AUTH_JWT_AUDIENCE = self.original_audience

    def _token_for(self, customer):
        return create_customer_access_token(customer.id, customer.token_version)[0]

    def _authorization(self, customer):
        return {"Authorization": f"Bearer {self._token_for(customer)}"}

    def _seed_chat_confirmation(self, session_id):
        chat_agent.memory_manager.clear_session(session_id)
        chat_agent.memory_manager.update_session(
            session_id,
            {
                "intent": "reservation",
                "intent_confidence": 0.95,
                "name": "Rizal",
                "people": 4,
                "date": "2026-08-01",
                "time": "19:00",
                "awaiting_confirmation": True,
            },
        )

    def _confirm_chat_reservation(self, session_id, customer, create_reservation):
        self._seed_chat_confirmation(session_id)
        response = self.client.post(
            "/chat",
            json={"session_id": session_id, "message": "Ya"},
            headers=self._authorization(customer),
        )
        chat_agent.memory_manager.clear_session(session_id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Reservasi berhasil dibuat", response.json()["reply"])
        self.assertEqual(
            create_reservation.call_args.kwargs["owner_customer_id"],
            customer.id,
        )

    def test_chat_rejects_missing_token(self):
        response = self.client.post(
            "/chat",
            json={"session_id": "memory-only", "message": "Saya mau reservasi"},
        )

        self.assertEqual(response.status_code, 401)

    def test_chat_rejects_invalid_token(self):
        forged_token = jwt.encode(
            {
                "sub": str(self.customer_a.id),
                "token_version": 1,
                "iat": 1_700_000_000,
                "exp": 4_000_000_000,
                "iss": settings.AUTH_JWT_ISSUER,
                "aud": settings.AUTH_JWT_AUDIENCE,
            },
            "wrong-secret",
            algorithm=JWT_ALGORITHM,
        )

        response = self.client.post(
            "/chat",
            json={"session_id": "memory-only", "message": "Saya mau reservasi"},
            headers={"Authorization": f"Bearer {forged_token}"},
        )

        self.assertEqual(response.status_code, 401)

    def test_valid_token_allows_chat_create_and_uses_authenticated_owner(self):
        reservation_db = MagicMock()
        with (
            patch("app.agents.reservation_agent.SessionLocal", return_value=reservation_db),
            patch(
                "app.agents.reservation_agent.ReservationService.create_reservation",
                return_value=MagicMock(),
            ) as create_reservation,
        ):
            self._confirm_chat_reservation("chat-customer-a", self.customer_a, create_reservation)

        self.assertNotIn("customer_id", create_reservation.call_args.kwargs)
        reservation_db.close.assert_called_once()

    def test_same_token_with_different_session_ids_keeps_same_owner(self):
        reservation_db = MagicMock()
        with (
            patch("app.agents.reservation_agent.SessionLocal", return_value=reservation_db),
            patch(
                "app.agents.reservation_agent.ReservationService.create_reservation",
                return_value=MagicMock(),
            ) as create_reservation,
        ):
            self._confirm_chat_reservation("chat-session-one", self.customer_a, create_reservation)
            self._confirm_chat_reservation("chat-session-two", self.customer_a, create_reservation)

        owners = [
            call.kwargs["owner_customer_id"]
            for call in create_reservation.call_args_list
        ]
        self.assertEqual(owners, [self.customer_a.id, self.customer_a.id])

    def test_different_customer_tokens_create_distinct_owners(self):
        reservation_db = MagicMock()
        with (
            patch("app.agents.reservation_agent.SessionLocal", return_value=reservation_db),
            patch(
                "app.agents.reservation_agent.ReservationService.create_reservation",
                return_value=MagicMock(),
            ) as create_reservation,
        ):
            self._confirm_chat_reservation("chat-customer-a", self.customer_a, create_reservation)
            self._confirm_chat_reservation("chat-customer-b", self.customer_b, create_reservation)

        owners = [
            call.kwargs["owner_customer_id"]
            for call in create_reservation.call_args_list
        ]
        self.assertEqual(owners, [self.customer_a.id, self.customer_b.id])

    def test_direct_reservation_requires_bearer_token(self):
        response = self.client.post(
            "/reservation/",
            json={
                "name": "Rizal",
                "people": 4,
                "date": "2026-08-01",
                "time": "19:00",
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_direct_reservation_ignores_x_session_id_for_secure_owner(self):
        created_reservation = SimpleNamespace(
            id=99,
            name="Rizal",
            people=4,
            date="2026-08-01",
            time="19:00",
            status="pending",
        )
        with patch(
            "app.api.reservation.service.create_reservation",
            return_value=created_reservation,
        ) as create_reservation:
            response = self.client.post(
                "/reservation/",
                json={
                    "name": "Rizal",
                    "people": 4,
                    "date": "2026-08-01",
                    "time": "19:00",
                },
                headers={
                    **self._authorization(self.customer_a),
                    "X-Session-ID": "attempted-owner-override",
                },
            )

        self.assertEqual(response.status_code, 200)
        create_reservation.assert_called_once()
        self.assertEqual(
            create_reservation.call_args.kwargs["owner_customer_id"],
            self.customer_a.id,
        )
        self.assertNotIn("customer_id", create_reservation.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
