"""Contract and safety tests for the hidden internal demo chat endpoint."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.internal_demo_chat import get_demo_chat_service
from app.api.internal_demo_dependencies import get_demo_rate_limit_service
from app.core.config import get_demo_settings
from app.db.database import get_db
from app.main import create_app
from app.schemas.demo_chat import (
    DemoChatHandoff,
    DemoChatReply,
    DemoChatResponse,
)
from app.services.demo_chat_service import (
    DemoChatProviderError,
    DemoChatProviderTimeoutError,
    DemoChatRequestConflictError,
    DemoChatServiceUnavailableError,
)
from app.services.demo_session_service import DemoSessionRequiredError


SERVICE_TOKEN = "safe-bff-service-token-for-chat-tests-2026"
SESSION_TOKEN = "D" * 43
REQUEST_ID = "61d831fc-2708-4693-a008-3f09f906be7a"


class _AllowingRateLimits:
    def resolve_active_session_digest(self, _db, _raw_token):
        return "b" * 64

    def enforce(self, _db, **_values):
        return ()


class _StubDemoChatService:
    def __init__(self):
        self.calls = []
        self.error = None
        self.handoff = None

    async def process(
        self,
        db,
        *,
        raw_session_token,
        message,
        request_id,
    ):
        if self.error is not None:
            raise self.error
        self.calls.append(
            (db, raw_session_token, message, request_id)
        )
        return DemoChatResponse(
            reply=DemoChatReply(
                id=12,
                role="assistant",
                content="Respons demo tersimpan.",
                created_at=datetime(
                    2026,
                    8,
                    1,
                    10,
                    5,
                    1,
                    tzinfo=timezone.utc,
                ),
            ),
            reservation_mutation=None,
            handoff=self.handoff,
        )


class InternalDemoChatAPITests(unittest.TestCase):
    def setUp(self):
        self.service = _StubDemoChatService()
        self.db = object()
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
        self.app.dependency_overrides[get_demo_chat_service] = (
            lambda: self.service
        )
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

    @staticmethod
    def body(message="Halo"):
        return {"message": message, "requestId": REQUEST_ID}

    def post(self, **kwargs):
        kwargs.setdefault("headers", self.headers())
        kwargs.setdefault("json", self.body())
        return self.client.post("/internal/demo/chat", **kwargs)

    def test_valid_request_returns_allowlisted_persisted_reply(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "reply": {
                    "id": 12,
                    "role": "assistant",
                    "content": "Respons demo tersimpan.",
                    "createdAt": "2026-08-01T10:05:01Z",
                },
                "reservationMutation": None,
                "handoff": None,
            },
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        call = self.service.calls[0]
        self.assertIs(call[0], self.db)
        self.assertEqual(call[1], SESSION_TOKEN)
        self.assertEqual(call[2], "Halo")
        self.assertEqual(call[3], UUID(REQUEST_ID))

    def test_handoff_response_exposes_status_only(self):
        self.service.handoff = DemoChatHandoff(status="simulated")
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["handoff"], {"status": "simulated"})
        self.assertNotIn("reference", response.text.casefold())

    def test_message_newlines_are_canonicalized_without_trimming(self):
        response = self.post(json=self.body("  satu\r\ndua\r  "))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[0][2], "  satu\ndua\n  ")

    def test_service_token_missing_and_invalid_are_rejected(self):
        missing = self.client.post(
            "/internal/demo/chat",
            headers={"X-Demo-Session-Token": SESSION_TOKEN},
            json=self.body(),
        )
        invalid = self.client.post(
            "/internal/demo/chat",
            headers={
                "X-BFF-Service-Token": "wrong",
                "X-Demo-Session-Token": SESSION_TOKEN,
            },
            json=self.body(),
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(missing.json(), invalid.json())
        self.assertEqual(
            missing.json()["code"],
            "DEMO_SERVICE_AUTH_REQUIRED",
        )
        self.assertEqual(self.service.calls, [])

    def test_session_token_missing_and_malformed_are_rejected(self):
        missing = self.client.post(
            "/internal/demo/chat",
            headers={"X-BFF-Service-Token": SERVICE_TOKEN},
            json=self.body(),
        )
        malformed = self.client.post(
            "/internal/demo/chat",
            headers={
                "X-BFF-Service-Token": SERVICE_TOKEN,
                "X-Demo-Session-Token": "unsafe token",
            },
            json=self.body(),
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(malformed.status_code, 401)
        self.assertEqual(missing.json(), malformed.json())
        self.assertEqual(missing.json()["code"], "DEMO_SESSION_REQUIRED")

    def test_revoked_or_expired_session_is_reported_safely(self):
        self.service.error = DemoSessionRequiredError()
        response = self.post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "DEMO_SESSION_REQUIRED")
        rendered = response.text.casefold()
        self.assertNotIn(SESSION_TOKEN.casefold(), rendered)

    def test_empty_and_whitespace_messages_are_rejected(self):
        for message in ("", " ", "\r\n\t"):
            with self.subTest(message=repr(message)):
                response = self.post(json=self.body(message))
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertEqual(self.service.calls, [])

    def test_exactly_one_thousand_unicode_code_points_is_accepted(self):
        message = "🙂" * 1000
        response = self.post(json=self.body(message))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[0][2], message)
        self.assertEqual(len(self.service.calls[0][2]), 1000)

    def test_more_than_one_thousand_unicode_code_points_is_rejected(self):
        response = self.post(json=self.body("🙂" * 1001))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertEqual(
            response.json()["errors"][0]["code"],
            "CHAT_MESSAGE_TOO_LONG",
        )

    def test_message_must_be_a_string(self):
        for value in (None, 7, True, ["Halo"], {"text": "Halo"}):
            with self.subTest(value=value):
                response = self.post(json=self.body(value))
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(repr(value), response.text)

    def test_request_id_is_required_and_must_be_uuid(self):
        missing = self.post(json={"message": "Halo"})
        malformed = self.post(
            json={"message": "Halo", "requestId": "not-a-uuid"}
        )
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(malformed.status_code, 422)
        self.assertNotIn("not-a-uuid", malformed.text)

    def test_extra_and_identity_fields_are_forbidden(self):
        forbidden_fields = (
            "ownerId",
            "customerId",
            "sessionId",
            "conversationId",
            "token",
            "role",
            "systemPrompt",
            "tool",
            "provider",
            "databaseId",
        )
        for field in forbidden_fields:
            with self.subTest(field=field):
                body = self.body()
                body[field] = "attacker-controlled"
                response = self.post(json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["errors"][0]["code"],
                    "EXTRA_FIELD_FORBIDDEN",
                )

    def test_non_object_bodies_are_rejected_without_reflection(self):
        for body in ([], "secret-message", 42):
            with self.subTest(body=body):
                response = self.post(json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("secret-message", response.text)

    def test_malformed_json_has_safe_validation_error(self):
        response = self.client.post(
            "/internal/demo/chat",
            headers={
                **self.headers(),
                "Content-Type": "application/json",
            },
            content='{"message":"private raw marker",',
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertNotIn("private raw marker", response.text)

    def test_route_is_hidden_from_openapi(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertNotIn("/internal/demo/chat", paths)

    def test_route_does_not_exist_outside_demo(self):
        app = create_app(
            SimpleNamespace(
                APP_ENV="production",
                APP_NAME="AURA",
                VERSION="test",
            )
        )
        with TestClient(app) as client:
            response = client.post(
                "/internal/demo/chat",
                headers=self.headers(),
                json=self.body(),
            )
        self.assertEqual(response.status_code, 404)

    def test_guest_jwt_does_not_replace_service_authentication(self):
        response = self.client.post(
            "/internal/demo/chat",
            headers={
                "Authorization": "Bearer guest-jwt-marker",
                "X-Demo-Session-Token": SESSION_TOKEN,
            },
            json=self.body(),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["code"],
            "DEMO_SERVICE_AUTH_REQUIRED",
        )
        self.assertNotIn("guest-jwt-marker", response.text)

    def test_response_never_contains_internal_identity_or_tokens(self):
        response = self.post()
        rendered = response.text.casefold()
        for forbidden in (
            "owner",
            "customer",
            "demo_session",
            "sessiontoken",
            "token_digest",
            "bff",
            "jwt",
            "workflow",
            "provider",
            "systemprompt",
            "sql",
            "database",
            "telegram",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_safe_service_error_mapping(self):
        cases = (
            (DemoChatRequestConflictError(), 409, "REQUEST_CONFLICT"),
            (DemoChatProviderError(), 502, "PROVIDER_ERROR"),
            (
                DemoChatServiceUnavailableError(),
                503,
                "SERVICE_UNAVAILABLE",
            ),
            (
                DemoChatProviderTimeoutError(),
                504,
                "PROVIDER_TIMEOUT",
            ),
        )
        for error, status_code, code in cases:
            with self.subTest(code=code):
                self.service.error = error
                response = self.post()
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn(repr(error), response.text)
                self.assertEqual(
                    response.headers["cache-control"],
                    "no-store",
                )

    def test_request_conflict_does_not_disclose_previous_message(self):
        previous_message = "pesan lama yang sensitif"
        self.service.error = DemoChatRequestConflictError()
        response = self.post(json=self.body("pesan baru"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "REQUEST_CONFLICT")
        self.assertNotIn(previous_message, response.text)
        self.assertNotIn("pesan baru", response.text)

    def test_query_tokens_do_not_satisfy_header_dependencies(self):
        response = self.client.post(
            (
                "/internal/demo/chat"
                f"?serviceToken={SERVICE_TOKEN}&sessionToken={SESSION_TOKEN}"
            ),
            json=self.body(),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["code"],
            "DEMO_SERVICE_AUTH_REQUIRED",
        )

    def test_existing_production_chat_route_remains_registered_once(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("/chat", paths)
        self.assertEqual(list(paths).count("/chat"), 1)


if __name__ == "__main__":
    unittest.main()
