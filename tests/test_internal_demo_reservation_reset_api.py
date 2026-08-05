"""Contract tests for hidden demo reservation read and reset endpoints."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.internal_demo_reservation_reset import (
    get_demo_reservation_reset_service,
)
from app.api.internal_demo_dependencies import get_demo_rate_limit_service
from app.core.config import get_demo_settings
from app.db.database import get_db
from app.main import create_app
from app.schemas.demo_reservation_reset import (
    DemoReservationItem,
    DemoReservationListResponse,
    DemoResetResponse,
)
from app.schemas.demo_session import DemoSessionSummary
from app.services.demo_chat_errors import (
    DemoChatRequestConflictError,
    DemoChatServiceUnavailableError,
)
from app.services.demo_session_service import DemoSessionRequiredError


SERVICE_TOKEN = "safe-bff-service-token-for-reset-tests-2026"
SESSION_TOKEN = "U" * 43


class _AllowingRateLimits:
    def resolve_active_session_digest(self, _db, _raw_token):
        return "c" * 64

    def enforce(self, _db, **_values):
        return ()


class _StubReservationResetService:
    def __init__(self):
        self.list_calls = []
        self.reset_calls = []
        self.list_error = None
        self.reset_error = None
        now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        self.session = DemoSessionSummary(
            expires_at=now + timedelta(hours=2),
            idle_expires_at=now + timedelta(hours=2),
            absolute_expires_at=now + timedelta(hours=24),
            message_count=0,
        )

    def list_reservations(self, db, *, raw_session_token):
        if self.list_error is not None:
            raise self.list_error
        self.list_calls.append((db, raw_session_token))
        return DemoReservationListResponse(
            reservations=(
                DemoReservationItem(
                    reservation_reference="RSV_11111111111111111111111111111111",
                    status="pending",
                    reservation_date="2026-08-03",
                    reservation_time="19:00",
                    party_size=4,
                ),
            ),
            count=1,
        )

    async def reset(self, db, *, raw_session_token):
        if self.reset_error is not None:
            raise self.reset_error
        self.reset_calls.append((db, raw_session_token))
        return DemoResetResponse(session=self.session)


class InternalDemoReservationResetAPITests(unittest.TestCase):
    def setUp(self):
        self.service = _StubReservationResetService()
        self.db = object()
        self.settings = SimpleNamespace(
            APP_ENV="demo",
            APP_NAME="AURA",
            VERSION="test",
        )
        self.app = create_app(self.settings)
        self.app.dependency_overrides[get_demo_settings] = lambda: (
            SimpleNamespace(
                APP_ENV="demo",
                DEMO_BFF_SERVICE_TOKEN=SecretStr(SERVICE_TOKEN),
            )
        )
        self.app.dependency_overrides[
            get_demo_reservation_reset_service
        ] = lambda: self.service
        self.app.dependency_overrides[get_demo_rate_limit_service] = (
            _AllowingRateLimits
        )
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.client.close()

    @staticmethod
    def headers():
        return {
            "X-BFF-Service-Token": SERVICE_TOKEN,
            "X-Demo-Session-Token": SESSION_TOKEN,
        }

    def test_valid_reservation_read_is_allowlisted_and_no_store(self):
        response = self.client.get(
            "/internal/demo/reservations",
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "reservations": [
                {
                    "reservationReference": (
                        "RSV_11111111111111111111111111111111"
                    ),
                    "status": "pending",
                        "reservationDate": "2026-08-03",
                        "reservationTime": "19:00:00",
                        "partySize": 4,
                    }
                ],
                "count": 1,
            },
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.service.list_calls, [(self.db, SESSION_TOKEN)])
        for internal_name in (
            "id",
            "reference",
            "reservationId",
            "ownerId",
            "customerId",
            "sessionId",
        ):
            self.assertNotIn(internal_name, response.text)

    def test_valid_reset_is_stable_allowlisted_and_no_store(self):
        response = self.client.post(
            "/internal/demo/reset",
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "reset")
        self.assertEqual(body["session"]["status"], "active")
        self.assertEqual(body["session"]["messageCount"], 0)
        self.assertEqual(body["reservationCount"], 0)
        self.assertIsNone(body["handoff"])
        self.assertNotIn("sessionToken", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.service.reset_calls, [(self.db, SESSION_TOKEN)])

    def test_reset_accepts_explicit_empty_bytes(self):
        response = self.client.post(
            "/internal/demo/reset",
            headers=self.headers(),
            content=b"",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.service.reset_calls, [(self.db, SESSION_TOKEN)])

    def test_missing_and_invalid_service_tokens_are_rejected(self):
        for method, path in (
            ("get", "/internal/demo/reservations"),
            ("post", "/internal/demo/reset"),
        ):
            with self.subTest(method=method):
                missing = getattr(self.client, method)(
                    path,
                    headers={"X-Demo-Session-Token": SESSION_TOKEN},
                )
                invalid = getattr(self.client, method)(
                    path,
                    headers={
                        "X-BFF-Service-Token": "wrong",
                        "X-Demo-Session-Token": SESSION_TOKEN,
                    },
                )
                self.assertEqual(missing.status_code, 401)
                self.assertEqual(invalid.status_code, 401)
                self.assertEqual(missing.json(), invalid.json())
                self.assertEqual(
                    missing.json()["code"],
                    "DEMO_SERVICE_AUTH_REQUIRED",
                )

    def test_missing_and_invalid_session_tokens_are_rejected(self):
        for method, path in (
            ("get", "/internal/demo/reservations"),
            ("post", "/internal/demo/reset"),
        ):
            with self.subTest(method=method):
                missing = getattr(self.client, method)(
                    path,
                    headers={"X-BFF-Service-Token": SERVICE_TOKEN},
                )
                invalid = getattr(self.client, method)(
                    path,
                    headers={
                        "X-BFF-Service-Token": SERVICE_TOKEN,
                        "X-Demo-Session-Token": "not valid",
                    },
                )
                self.assertEqual(missing.status_code, 401)
                self.assertEqual(invalid.status_code, 401)
                self.assertEqual(missing.json(), invalid.json())
                self.assertEqual(
                    missing.json()["code"],
                    "DEMO_SESSION_REQUIRED",
                )

    def test_revoked_or_expired_session_is_safe_401(self):
        self.service.list_error = DemoSessionRequiredError()
        response = self.client.get(
            "/internal/demo/reservations",
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "DEMO_SESSION_REQUIRED")
        self.assertNotIn(SESSION_TOKEN, response.text)

    def test_reset_conflict_and_persistence_failure_are_safe(self):
        self.service.reset_error = DemoChatRequestConflictError()
        conflict = self.client.post(
            "/internal/demo/reset",
            headers=self.headers(),
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "REQUEST_CONFLICT")
        self.service.reset_error = DemoChatServiceUnavailableError()
        unavailable = self.client.post(
            "/internal/demo/reset",
            headers=self.headers(),
        )
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["code"], "SERVICE_UNAVAILABLE")
        self.assertNotIn(SESSION_TOKEN, unavailable.text)

    def test_query_identity_fields_are_rejected(self):
        for path in (
            "/internal/demo/reservations?ownerId=17",
            "/internal/demo/reservations?customerId=17",
            "/internal/demo/reservations?sessionId=17",
            "/internal/demo/reservations?unexpected=value",
            "/internal/demo/reset?ownerId=17",
            "/internal/demo/reset?unexpected=value",
        ):
            with self.subTest(path=path):
                method = self.client.post if "/reset" in path else self.client.get
                response = method(path, headers=self.headers())
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
                self.assertEqual(
                    response.headers["cache-control"],
                    "no-store",
                )

    def test_reset_rejects_every_nonempty_body_without_state_change(self):
        bodies = (
            b"{}",
            b'{"unexpected":"do-not-reflect-marker"}',
            b'["do-not-reflect-marker"]',
            b'"do-not-reflect-marker"',
            b"17",
            b"true",
            b"{",
        )
        before = (
            tuple(self.service.list_calls),
            tuple(self.service.reset_calls),
        )
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.post(
                    "/internal/demo/reset",
                    headers={
                        **self.headers(),
                        "Content-Type": "application/json",
                    },
                    content=body,
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["code"],
                    "VALIDATION_ERROR",
                )
                self.assertNotIn("do-not-reflect-marker", response.text)
                self.assertEqual(
                    (
                        tuple(self.service.list_calls),
                        tuple(self.service.reset_calls),
                    ),
                    before,
                )

    def test_get_rejects_query_and_nonempty_bodies_without_state_change(self):
        bodies = (
            b"{}",
            b'["do-not-reflect-marker"]',
            b'"do-not-reflect-marker"',
            b"17",
        )
        before = (
            tuple(self.service.list_calls),
            tuple(self.service.reset_calls),
        )
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.request(
                    "GET",
                    "/internal/demo/reservations",
                    headers={
                        **self.headers(),
                        "Content-Type": "application/json",
                    },
                    content=body,
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["code"],
                    "VALIDATION_ERROR",
                )
                self.assertNotIn("do-not-reflect-marker", response.text)
        query = self.client.get(
            "/internal/demo/reservations?anything=value",
            headers=self.headers(),
        )
        self.assertEqual(query.status_code, 422)
        self.assertEqual(
            (
                tuple(self.service.list_calls),
                tuple(self.service.reset_calls),
            ),
            before,
        )

    def test_invalid_body_does_not_bypass_service_token(self):
        response = self.client.post(
            "/internal/demo/reset",
            headers={"Content-Type": "application/json"},
            content=b'{"unexpected":"do-not-reflect-marker"}',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["code"],
            "DEMO_SERVICE_AUTH_REQUIRED",
        )
        self.assertNotIn("do-not-reflect-marker", response.text)
        self.assertEqual(self.service.reset_calls, [])

    def test_routes_are_hidden_from_openapi(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertNotIn("/internal/demo/reservations", paths)
        self.assertNotIn("/internal/demo/reset", paths)

    def test_non_demo_routes_are_404(self):
        app = create_app(
            SimpleNamespace(APP_ENV="production", APP_NAME="AURA", VERSION="test")
        )
        client = TestClient(app)
        try:
            for method, path in (
                ("get", "/internal/demo/reservations"),
                ("post", "/internal/demo/reset"),
            ):
                response = getattr(client, method)(path, headers=self.headers())
                self.assertEqual(response.status_code, 404)
        finally:
            client.close()

    def test_guest_jwt_cannot_replace_service_or_session_headers(self):
        authorization = {"Authorization": "Bearer guest-token"}
        for method, path in (
            ("get", "/internal/demo/reservations"),
            ("post", "/internal/demo/reset"),
        ):
            response = getattr(self.client, method)(
                path,
                headers=authorization,
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(
                response.json()["code"],
                "DEMO_SERVICE_AUTH_REQUIRED",
            )

    def test_response_schema_forbids_internal_fields_and_statuses(self):
        with self.assertRaises(ValidationError):
            DemoReservationItem(
                reservation_reference="RSV_11111111111111111111111111111111",
                status="confirmed",
                reservation_date="2026-08-03",
                reservation_time="19:00",
                party_size=4,
            )
        for field_name in (
            "id",
            "reference",
            "reservationId",
            "ownerId",
            "customerId",
            "sessionId",
        ):
            with self.subTest(field_name=field_name), self.assertRaises(
                ValidationError
            ):
                DemoReservationItem(
                    reservation_reference="RSV_11111111111111111111111111111111",
                    status="pending",
                    reservation_date="2026-08-03",
                    reservation_time="19:00",
                    party_size=4,
                    **{field_name: "unsafe"},
                )
        with self.assertRaises(ValidationError):
            DemoReservationListResponse(
                reservations=(),
                count=0,
                owner_customer_id="unsafe",
            )
        with self.assertRaises(ValidationError):
            DemoResetResponse(
                session=self.service.session,
                session_token="unsafe",
            )

    def test_production_chat_route_remains_registered(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("/chat", paths)


if __name__ == "__main__":
    unittest.main()
