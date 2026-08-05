"""Contract tests for the hidden BFF-to-AURA demo-session API."""

from datetime import datetime, timedelta, timezone
import hmac
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.internal_demo_dependencies import (
    digest_demo_service_token,
    get_demo_rate_limit_service,
    get_demo_session_service,
)
from app.core.config import get_demo_settings
from app.core.transaction_errors import PersistenceOperationError
from app.db.database import get_db
from app.main import create_app
from app.schemas.demo_session import (
    DemoSessionCreateResponse,
    DemoSessionCurrentResponse,
    DemoSessionMessage,
    DemoSessionHandoff,
    DemoSessionSummary,
)
from app.services.demo_chat_errors import DemoHistoryResetRequiredError
from app.services.demo_session_service import DemoSessionRequiredError


SERVICE_TOKEN = "safe-bff-service-token-for-api-tests-2026"
SESSION_TOKEN = "S" * 43


class _AllowingRateLimits:
    def resolve_active_session_digest(self, _db, _raw_token):
        return "a" * 64

    def enforce(self, _db, **_values):
        return ()


class _StubDemoSessionService:
    def __init__(self):
        self.now = datetime(2026, 7, 29, 4, 0, tzinfo=timezone.utc)
        self.created = 0
        self.current_tokens = []
        self.create_error = None
        self.current_error = None
        self.handoff = None

    def summary(self, message_count=0):
        return DemoSessionSummary(
            expires_at=self.now + timedelta(hours=2),
            idle_expires_at=self.now + timedelta(hours=2),
            absolute_expires_at=self.now + timedelta(hours=24),
            message_count=message_count,
        )

    def create_session(self, _db):
        if self.create_error is not None:
            raise self.create_error
        self.created += 1
        return DemoSessionCreateResponse(
            session_token=SESSION_TOKEN,
            session=self.summary(),
        )

    def get_current_session(self, _db, raw_session_token):
        if self.current_error is not None:
            raise self.current_error
        self.current_tokens.append(raw_session_token)
        return DemoSessionCurrentResponse(
            session=self.summary(message_count=1),
            messages=(
                DemoSessionMessage(
                    id=1,
                    role="user",
                    content="Pesan contoh.",
                    created_at=self.now,
                ),
            ),
            handoff=self.handoff,
        )


class InternalDemoSessionAPITests(unittest.TestCase):
    def setUp(self):
        self.service = _StubDemoSessionService()
        settings = SimpleNamespace(
            APP_ENV="demo",
            APP_NAME="AURA",
            VERSION="test",
        )
        self.app = create_app(settings)
        self.app.dependency_overrides[get_demo_settings] = lambda: (
            SimpleNamespace(
                APP_ENV="demo",
                DEMO_BFF_SERVICE_TOKEN=SecretStr(SERVICE_TOKEN),
            )
        )
        self.app.dependency_overrides[get_demo_session_service] = (
            lambda: self.service
        )
        self.app.dependency_overrides[get_demo_rate_limit_service] = (
            _AllowingRateLimits
        )
        self.app.dependency_overrides[get_db] = lambda: object()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.client.close()

    @staticmethod
    def service_headers():
        return {"X-BFF-Service-Token": SERVICE_TOKEN}

    @staticmethod
    def current_headers():
        return {
            "X-BFF-Service-Token": SERVICE_TOKEN,
            "X-Demo-Session-Token": SESSION_TOKEN,
        }

    def test_post_with_valid_service_token_returns_201(self):
        response = self.client.post(
            "/internal/demo/sessions",
            headers=self.service_headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["sessionToken"], SESSION_TOKEN)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.service.created, 1)

    def test_missing_and_wrong_service_tokens_are_indistinguishable(self):
        missing = self.client.post("/internal/demo/sessions")
        wrong_tokens = (
            "X" * len(SERVICE_TOKEN),
            "short",
            "X" * (len(SERVICE_TOKEN) + 20),
        )
        responses = [
            self.client.post(
                "/internal/demo/sessions",
                headers={"X-BFF-Service-Token": token},
            )
            for token in wrong_tokens
        ]
        self.assertEqual(missing.status_code, 401)
        for response in responses:
            self.assertEqual(response.status_code, 401)
            self.assertEqual(missing.json(), response.json())
        self.assertEqual(
            missing.json()["code"],
            "DEMO_SERVICE_AUTH_REQUIRED",
        )
        self.assertEqual(
            missing.json()["detail"],
            "Akses layanan demo tidak valid.",
        )

    def test_service_token_comparison_uses_fixed_length_digests(self):
        self.assertEqual(len(digest_demo_service_token("short")), 32)
        self.assertEqual(
            len(digest_demo_service_token("a-much-longer-token-value")),
            32,
        )
        with patch(
            "app.api.internal_demo_dependencies.hmac.compare_digest",
            wraps=hmac.compare_digest,
        ) as compare_digest:
            response = self.client.post(
                "/internal/demo/sessions",
                headers=self.service_headers(),
            )
        self.assertEqual(response.status_code, 201)
        presented, configured = compare_digest.call_args.args
        self.assertEqual(len(presented), 32)
        self.assertEqual(len(configured), 32)

    def test_service_tokens_are_absent_from_errors_and_captured_logs(self):
        invalid_token = "logged-invalid-service-token-marker"
        with self.assertLogs(level=logging.INFO) as captured:
            response = self.client.post(
                "/internal/demo/sessions",
                headers={"X-BFF-Service-Token": invalid_token},
            )
        rendered = (
            response.text
            + "\n".join(captured.output)
            + repr(response.json())
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(invalid_token, rendered)
        self.assertNotIn(SERVICE_TOKEN, rendered)

    def test_guest_bearer_jwt_cannot_replace_service_token(self):
        response = self.client.post(
            "/internal/demo/sessions",
            headers={"Authorization": "Bearer guest-jwt"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["code"],
            "DEMO_SERVICE_AUTH_REQUIRED",
        )

    def test_get_with_both_headers_returns_safe_200(self):
        response = self.client.get(
            "/internal/demo/sessions/current",
            headers=self.current_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.current_tokens, [SESSION_TOKEN])
        payload = response.json()
        self.assertEqual(payload["session"]["messageCount"], 1)
        self.assertEqual(payload["messages"][0]["content"], "Pesan contoh.")
        self.assertIsNone(payload["handoff"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_current_handoff_omits_internal_reference(self):
        self.service.handoff = DemoSessionHandoff(
            status="simulated",
            reason_code="explicit_human_request",
            safe_summary="Demo visitor requested simulated human assistance.",
            created_at=self.service.now,
        )
        response = self.client.get(
            "/internal/demo/sessions/current",
            headers=self.current_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["handoff"],
            {
                "status": "simulated",
                "reasonCode": "explicit_human_request",
                "safeSummary": (
                    "Demo visitor requested simulated human assistance."
                ),
                "createdAt": "2026-07-29T04:00:00Z",
            },
        )
        self.assertNotIn("reference", response.text.casefold())

    def test_current_without_session_header_returns_safe_401(self):
        response = self.client.get(
            "/internal/demo/sessions/current",
            headers=self.service_headers(),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "DEMO_SESSION_REQUIRED")
        self.assertEqual(self.service.current_tokens, [])

    def test_current_with_malformed_session_header_returns_safe_401(self):
        for invalid in ("", "short", "contains whitespace"):
            with self.subTest(invalid=repr(invalid)):
                response = self.client.get(
                    "/internal/demo/sessions/current",
                    headers={
                        **self.service_headers(),
                        "X-Demo-Session-Token": invalid,
                    },
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.json()["code"],
                    "DEMO_SESSION_REQUIRED",
                )
                if invalid:
                    self.assertNotIn(invalid, response.text)

    def test_random_valid_session_token_is_rejected_without_disclosure(self):
        random_token = "R" * 43
        self.service.current_error = DemoSessionRequiredError()
        response = self.client.get(
            "/internal/demo/sessions/current",
            headers={
                **self.service_headers(),
                "X-Demo-Session-Token": random_token,
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "DEMO_SESSION_REQUIRED")
        self.assertNotIn(random_token, response.text)

    def test_legacy_history_returns_safe_reset_required_envelope(self):
        self.service.current_error = DemoHistoryResetRequiredError()
        response = self.client.get(
            "/internal/demo/sessions/current",
            headers=self.current_headers(),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "detail": (
                    "Riwayat demo lama harus direset sebelum sesi dapat "
                    "dilanjutkan."
                ),
                "code": "DEMO_HISTORY_RESET_REQUIRED",
            },
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(SESSION_TOKEN, response.text)

    def test_internal_routes_are_absent_from_openapi(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertNotIn("/internal/demo/sessions", paths)
        self.assertNotIn("/internal/demo/sessions/current", paths)

    def test_internal_routes_are_not_registered_outside_demo(self):
        app = create_app(
            SimpleNamespace(
                APP_ENV="production",
                APP_NAME="AURA",
                VERSION="test",
            )
        )
        with TestClient(app) as client:
            response = client.post(
                "/internal/demo/sessions",
                headers=self.service_headers(),
            )
        self.assertEqual(response.status_code, 404)

    def test_service_token_never_appears_in_create_response(self):
        response = self.client.post(
            "/internal/demo/sessions",
            headers=self.service_headers(),
        )
        self.assertNotIn(SERVICE_TOKEN, response.text)
        self.assertNotIn("serviceToken", response.text)

    def test_session_token_appears_only_in_create_response(self):
        created = self.client.post(
            "/internal/demo/sessions",
            headers=self.service_headers(),
        )
        current = self.client.get(
            "/internal/demo/sessions/current",
            headers=self.current_headers(),
        )
        self.assertIn(SESSION_TOKEN, created.text)
        self.assertNotIn(SESSION_TOKEN, current.text)
        self.assertNotIn("sessionToken", current.text)

    def test_existing_routes_remain_registered(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        for path in (
            "/",
            "/health",
            "/auth/guest",
            "/chat",
            "/reservation/",
        ):
            with self.subTest(path=path):
                self.assertIn(path, paths)

    def test_persistence_failure_has_safe_503_response(self):
        self.service.create_error = PersistenceOperationError()
        response = self.client.post(
            "/internal/demo/sessions",
            headers=self.service_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["code"],
            "PERSISTENCE_OPERATION_FAILED",
        )
        for forbidden in (
            SERVICE_TOKEN,
            "sql",
            "postgresql://",
            "password",
        ):
            self.assertNotIn(forbidden, response.text.casefold())

    def test_response_schema_forbids_internal_fields(self):
        now = datetime(2026, 7, 29, 4, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValidationError):
            DemoSessionSummary(
                expires_at=now,
                idle_expires_at=now,
                absolute_expires_at=now,
                message_count=0,
                owner_customer_id="internal",
            )

    def test_create_accepts_no_customer_or_owner_input(self):
        cases = (
            ({"json": {"customerId": "client-controlled"}}, "client-controlled"),
            ({"json": {"ownerId": "owner-controlled"}}, "owner-controlled"),
            ({"json": {"unexpected": "random-marker"}}, "random-marker"),
            ({"json": ["array-marker"]}, "array-marker"),
            ({"json": "string-marker"}, "string-marker"),
            (
                {
                    "content": '{"malformed":"payload-marker"',
                    "headers": {
                        **self.service_headers(),
                        "Content-Type": "application/json",
                    },
                },
                "payload-marker",
            ),
        )
        for request_values, marker in cases:
            with self.subTest(marker=marker):
                headers = request_values.pop(
                    "headers",
                    self.service_headers(),
                )
                response = self.client.post(
                    "/internal/demo/sessions",
                    headers=headers,
                    **request_values,
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["code"],
                    "REQUEST_VALIDATION_FAILED",
                )
                self.assertNotIn(marker, response.text)
        self.assertEqual(self.service.created, 0)

    def test_create_accepts_only_a_truly_empty_body(self):
        response = self.client.post(
            "/internal/demo/sessions",
            headers=self.service_headers(),
            content=b"",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.service.created, 1)

    def test_query_or_body_service_token_cannot_replace_header(self):
        query = self.client.post(
            f"/internal/demo/sessions?serviceToken={SERVICE_TOKEN}"
        )
        body = self.client.post(
            "/internal/demo/sessions",
            json={"serviceToken": SERVICE_TOKEN},
        )
        self.assertEqual(query.status_code, 401)
        self.assertEqual(body.status_code, 401)
        self.assertEqual(query.json(), body.json())
        self.assertNotIn(SERVICE_TOKEN, query.text)
        self.assertNotIn(SERVICE_TOKEN, body.text)
        self.assertEqual(self.service.created, 0)

    def test_session_query_parameter_cannot_replace_header(self):
        response = self.client.get(
            f"/internal/demo/sessions/current?sessionToken={SESSION_TOKEN}",
            headers=self.service_headers(),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "DEMO_SESSION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
