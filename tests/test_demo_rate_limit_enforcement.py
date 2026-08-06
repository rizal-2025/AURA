"""API enforcement order, safe failures, and protected-call isolation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.result import AgentTurnResult
from app.api.internal_demo_chat import get_demo_chat_service
from app.api.internal_demo_dependencies import (
    get_demo_rate_limit_service,
    get_demo_session_service,
)
from app.api.internal_demo_reservation_reset import (
    get_demo_reservation_reset_service,
)
from app.core.config import get_demo_settings
from app.core.unit_of_work import UnitOfWork
from app.db.base import Base
from app.db.database import get_db
from app.db.models.demo_persistence import (
    DemoChatMessage,
    DemoHandoffEvent,
    DemoRateLimitBucket,
    DemoSession,
)
from app.db.models.reservation import Reservation
from app.db.repositories.demo_persistence_repository import (
    DemoHandoffEventRepository,
    DemoRateLimitBucketRepository,
)
from app.main import create_app
from app.services.demo_chat_errors import DemoChatServiceUnavailableError
from app.services.demo_chat_service import DemoChatService
from app.services.demo_rate_limit_service import (
    DemoRateLimitAction,
    DemoRateLimitDecision,
    DemoRateLimitExceededError,
    DemoRateLimitService,
)
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    digest_demo_session_token,
)


SERVICE_TOKEN = "rate-enforcement-service-token-2026"
SESSION_TOKEN = "L" * 43
CLIENT_SUBJECT = "c" * 64


def _rejected():
    return DemoRateLimitExceededError(
        DemoRateLimitDecision(
            allowed=False,
            limit=5,
            current_count=6,
            remaining=0,
            retry_after_seconds=17,
            reset_at=datetime(2026, 8, 2, 10, 1, tzinfo=timezone.utc),
        )
    )


class _RateLimits:
    def __init__(self):
        self.calls = []
        self.resolve_error = None
        self.enforce_error = None

    def resolve_active_session_digest(self, db, raw_token):
        self.calls.append(("resolve", db, raw_token))
        if self.resolve_error:
            raise self.resolve_error
        return "a" * 64

    def enforce(self, db, **values):
        self.calls.append(("enforce", db, values))
        if self.enforce_error:
            raise self.enforce_error
        return ()


class _MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class _CountingDemoSessionService(DemoSessionService):
    def __init__(self, **values):
        super().__init__(**values)
        self.current_calls = 0

    def get_current_session(self, db, raw_session_token):
        self.current_calls += 1
        return super().get_current_session(db, raw_session_token)


class _NoopDatabaseLock:
    def acquire(self, _db, *, demo_session_id):
        return demo_session_id

    def release(self, _db, *, demo_session_id, lease):
        if lease != demo_session_id:
            raise AssertionError("unexpected test lease")


class _ReplayCore:
    def __init__(self, calls, now):
        self.calls = calls
        self.now = now

    async def process_turn(
        self,
        *,
        db,
        customer,
        session_reference,
        message,
    ):
        self.calls.append(message)
        demo_session_id = int(session_reference.rsplit("-", 1)[1])
        with UnitOfWork(db) as unit:
            db.add(
                Reservation(
                    name="Replay",
                    people=2,
                    date="2026-08-04",
                    time="19:00",
                    owner_customer_id=customer.id,
                )
            )
            DemoHandoffEventRepository().create_simulated(
                db,
                demo_session_id=demo_session_id,
                reference="DEMO-HO-RE91A123",
                reason_code="explicit_human_request",
                created_at=self.now(),
            )
            unit.commit()
        return AgentTurnResult(reply="Stored assistant reply.")


class _FailSecondPolicyBuckets:
    def __init__(self):
        self.delegate = DemoRateLimitBucketRepository()
        self.scopes = []

    def consume_atomic(self, db, **values):
        self.scopes.append(values["scope_type"])
        if values["scope_type"] == "session":
            raise RuntimeError("unsafe SQL detail")
        return self.delegate.consume_atomic(db, **values)


class DemoRateLimitEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.db = object()
        self.rate_limits = _RateLimits()
        self.sessions = Mock()
        self.chat = SimpleNamespace(process=AsyncMock())
        self.reservations = SimpleNamespace(
            list_reservations=Mock(),
            reset=AsyncMock(),
        )
        self.app = create_app(
            SimpleNamespace(APP_ENV="demo", APP_NAME="AURA", VERSION="test")
        )
        self.app.dependency_overrides[get_demo_settings] = lambda: (
            SimpleNamespace(
                APP_ENV="demo",
                DEMO_BFF_SERVICE_TOKEN=SecretStr(SERVICE_TOKEN),
            )
        )
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_demo_rate_limit_service] = (
            lambda: self.rate_limits
        )
        self.app.dependency_overrides[get_demo_session_service] = (
            lambda: self.sessions
        )
        self.app.dependency_overrides[get_demo_chat_service] = (
            lambda: self.chat
        )
        self.app.dependency_overrides[
            get_demo_reservation_reset_service
        ] = lambda: self.reservations
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    @staticmethod
    def headers(include_session=True, service_token=SERVICE_TOKEN):
        headers = {
            "X-BFF-Service-Token": service_token,
            "X-Demo-Client-Subject": CLIENT_SUBJECT,
        }
        if include_session:
            headers["X-Demo-Session-Token"] = SESSION_TOKEN
        return headers

    def test_invalid_service_auth_never_reaches_limiter(self):
        response = self.client.post(
            "/internal/demo/sessions",
            headers=self.headers(False, "wrong"),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.rate_limits.calls, [])

    def test_invalid_session_syntax_never_creates_session_bucket(self):
        response = self.client.post(
            "/internal/demo/chat",
            headers={
                "X-BFF-Service-Token": SERVICE_TOKEN,
                "X-Demo-Session-Token": "invalid token",
            },
            json={
                "message": "Halo",
                "requestId": "61d831fc-2708-4693-a008-3f09f906be7a",
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.rate_limits.calls, [])

    def test_session_create_limit_prevents_customer_session_creation(self):
        self.rate_limits.enforce_error = _rejected()
        response = self.client.post(
            "/internal/demo/sessions",
            headers=self.headers(False),
        )
        self.assertEqual(response.status_code, 429)
        self.sessions.create_session.assert_not_called()
        self.assertEqual(
            self.rate_limits.calls[0][2]["action"],
            DemoRateLimitAction.SESSION_CREATE,
        )

    def test_429_is_safe_no_store_and_has_positive_retry(self):
        marker = "a" * 64
        self.rate_limits.enforce_error = _rejected()
        response = self.client.post(
            "/internal/demo/sessions",
            headers=self.headers(False),
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "RATE_LIMIT_EXCEEDED")
        self.assertEqual(response.headers["retry-after"], "17")
        self.assertEqual(response.headers["x-ratelimit-limit"], "5")
        self.assertEqual(response.headers["x-ratelimit-remaining"], "0")
        self.assertEqual(
            response.headers["x-ratelimit-reset"],
            str(
                int(
                    datetime(
                        2026,
                        8,
                        2,
                        10,
                        1,
                        tzinfo=timezone.utc,
                    ).timestamp()
                )
            ),
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(marker, response.text)
        self.assertNotIn(SESSION_TOKEN, response.text)

    def test_current_limit_follows_resolution_and_prevents_service_call(self):
        self.rate_limits.enforce_error = _rejected()
        response = self.client.get(
            "/internal/demo/sessions/current",
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 429)
        self.sessions.get_current_session.assert_not_called()
        self.assertEqual(
            [item[0] for item in self.rate_limits.calls],
            ["resolve", "enforce"],
        )
        self.assertEqual(
            self.rate_limits.calls[1][2]["action"],
            DemoRateLimitAction.SESSION_CURRENT,
        )

    def test_inactive_session_stops_before_bucket_and_business_service(self):
        self.rate_limits.resolve_error = DemoSessionRequiredError()
        response = self.client.post(
            "/internal/demo/reset",
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            [item[0] for item in self.rate_limits.calls],
            ["resolve"],
        )
        self.reservations.reset.assert_not_awaited()

    def test_chat_limit_prevents_agent_service_and_counts_before_call(self):
        self.rate_limits.enforce_error = _rejected()
        response = self.client.post(
            "/internal/demo/chat",
            headers=self.headers(),
            json={
                "message": "Halo",
                "requestId": "61d831fc-2708-4693-a008-3f09f906be7a",
            },
        )
        self.assertEqual(response.status_code, 429)
        self.chat.process.assert_not_awaited()
        self.assertEqual(
            [item[0] for item in self.rate_limits.calls],
            ["resolve", "enforce"],
        )
        self.assertEqual(
            self.rate_limits.calls[1][2]["action"],
            DemoRateLimitAction.CHAT,
        )

    def test_reservation_limit_prevents_reservation_query(self):
        self.rate_limits.enforce_error = _rejected()
        response = self.client.get(
            "/internal/demo/reservations",
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 429)
        self.reservations.list_reservations.assert_not_called()
        self.assertEqual(
            self.rate_limits.calls[1][2]["action"],
            DemoRateLimitAction.RESERVATIONS_READ,
        )

    def test_reset_limit_prevents_lock_and_delete_service(self):
        self.rate_limits.enforce_error = _rejected()
        response = self.client.post(
            "/internal/demo/reset",
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 429)
        self.reservations.reset.assert_not_awaited()
        self.assertEqual(
            self.rate_limits.calls[1][2]["action"],
            DemoRateLimitAction.RESET,
        )

    def test_replayed_chat_attempt_is_charged_before_business_each_time(self):
        self.rate_limits.enforce_error = _rejected()
        request = {
            "message": "Replay yang sama",
            "requestId": "61d831fc-2708-4693-a008-3f09f906be7a",
        }
        responses = [
            self.client.post(
                "/internal/demo/chat",
                headers=self.headers(),
                json=request,
            )
            for _ in range(2)
        ]
        self.assertEqual(
            [response.status_code for response in responses],
            [429, 429],
        )
        self.assertEqual(
            [item[0] for item in self.rate_limits.calls],
            ["resolve", "enforce", "resolve", "enforce"],
        )
        self.chat.process.assert_not_awaited()

    def test_limiter_persistence_failure_is_safe_503(self):
        self.rate_limits.enforce_error = DemoChatServiceUnavailableError()
        response = self.client.post(
            "/internal/demo/sessions",
            headers=self.headers(False),
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "SERVICE_UNAVAILABLE")
        self.sessions.create_session.assert_not_called()


class DemoRateLimitActualAPIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.create_function("char_length", 1, len)
            connection.create_function(
                "jsonb_typeof",
                1,
                lambda _value: "object",
            )

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        self.clock = _MutableClock(
            datetime(2026, 8, 2, 10, 0, 1, tzinfo=timezone.utc)
        )
        self.sessions = _CountingDemoSessionService(
            token_generator=lambda: SESSION_TOKEN,
            clock=self.clock,
        )
        with self.Session() as db:
            self.sessions.create_session(db)
        self.rate_limits = DemoRateLimitService(
            session_service=self.sessions,
            clock=self.clock,
        )
        self.chat = SimpleNamespace(process=AsyncMock())
        self.app = create_app(
            SimpleNamespace(APP_ENV="demo", APP_NAME="AURA", VERSION="test")
        )
        self.app.dependency_overrides[get_demo_settings] = lambda: (
            SimpleNamespace(
                APP_ENV="demo",
                DEMO_BFF_SERVICE_TOKEN=SecretStr(SERVICE_TOKEN),
            )
        )

        def provide_db():
            with self.Session() as db:
                yield db

        self.app.dependency_overrides[get_db] = provide_db
        self.app.dependency_overrides[get_demo_session_service] = (
            lambda: self.sessions
        )
        self.app.dependency_overrides[get_demo_rate_limit_service] = (
            lambda: self.rate_limits
        )
        self.app.dependency_overrides[get_demo_chat_service] = (
            lambda: self.chat
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    @staticmethod
    def headers():
        return {
            "X-BFF-Service-Token": SERVICE_TOKEN,
            "X-Demo-Client-Subject": CLIENT_SUBJECT,
            "X-Demo-Session-Token": SESSION_TOKEN,
        }

    def test_current_uses_actual_limit_and_rejection_never_touches_session(self):
        responses = [
            self.client.get(
                "/internal/demo/sessions/current",
                headers=self.headers(),
            )
            for _ in range(60)
        ]
        self.assertEqual(
            [response.status_code for response in responses],
            [200] * 60,
        )
        with self.Session() as db:
            session = db.scalar(
                select(DemoSession).where(
                    DemoSession.token_digest
                    == digest_demo_session_token(SESSION_TOKEN)
                )
            )
            state_before_rejection = (
                session.last_seen_at,
                session.idle_expires_at,
                session.absolute_expires_at,
            )

        self.clock.value += timedelta(seconds=1)
        rejected = self.client.get(
            "/internal/demo/sessions/current",
            headers=self.headers(),
        )

        with self.Session() as db:
            session = db.scalar(
                select(DemoSession).where(
                    DemoSession.token_digest
                    == digest_demo_session_token(SESSION_TOKEN)
                )
            )
            state_after_rejection = (
                session.last_seen_at,
                session.idle_expires_at,
                session.absolute_expires_at,
            )
            bucket_count = db.scalar(
                select(DemoRateLimitBucket.request_count).where(
                    DemoRateLimitBucket.scope_type == "session",
                    DemoRateLimitBucket.action == "session_current",
                )
            )

        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.json()["code"], "RATE_LIMIT_EXCEEDED")
        self.assertEqual(bucket_count, 61)
        self.assertEqual(self.sessions.current_calls, 60)
        self.assertEqual(state_after_rejection, state_before_rejection)
        self.assertEqual(rejected.headers["x-ratelimit-limit"], "60")
        self.assertEqual(rejected.headers["x-ratelimit-remaining"], "0")
        self.assertGreater(int(rejected.headers["retry-after"]), 0)
        expected_reset = datetime(
            2026,
            8,
            2,
            10,
            1,
            tzinfo=timezone.utc,
        )
        self.assertEqual(
            rejected.headers["x-ratelimit-reset"],
            str(int(expected_reset.timestamp())),
        )
        self.assertEqual(rejected.headers["cache-control"], "no-store")
        rendered = rejected.text
        self.assertNotIn(SESSION_TOKEN, rendered)
        self.assertNotIn(digest_demo_session_token(SESSION_TOKEN), rendered)
        self.assertNotIn("session_current", rendered)

    def test_chat_replay_is_charged_without_duplicate_business_effects(self):
        core_calls = []
        chat_service = DemoChatService(
            session_service=self.sessions,
            database_lock=_NoopDatabaseLock(),
            core_factory=lambda _session_id: _ReplayCore(
                core_calls,
                self.clock,
            ),
            clock=self.clock,
        )
        self.app.dependency_overrides[get_demo_chat_service] = (
            lambda: chat_service
        )
        request = {
            "message": "Replay yang sama",
            "requestId": "61d831fc-2708-4693-a008-3f09f906be7a",
        }

        first = self.client.post(
            "/internal/demo/chat",
            headers=self.headers(),
            json=request,
        )
        self.assertEqual(first.status_code, 200)
        with self.Session() as db:
            first_counts = {
                row.scope_type: row.request_count
                for row in db.scalars(
                    select(DemoRateLimitBucket).where(
                        DemoRateLimitBucket.action == "chat"
                    )
                )
            }
        self.assertEqual(
            first_counts,
            {"ip": 1, "session": 1, "global": 1},
        )

        accepted_replays = [
            self.client.post(
                "/internal/demo/chat",
                headers=self.headers(),
                json=request,
            )
            for _ in range(19)
        ]
        self.assertTrue(
            all(response.status_code == 200 for response in accepted_replays)
        )
        self.assertTrue(
            all(response.json() == first.json() for response in accepted_replays)
        )
        rejected = self.client.post(
            "/internal/demo/chat",
            headers=self.headers(),
            json=request,
        )

        with self.Session() as db:
            rate_counts = {
                row.scope_type: row.request_count
                for row in db.scalars(
                    select(DemoRateLimitBucket).where(
                        DemoRateLimitBucket.action == "chat"
                    )
                )
            }
            role_counts = dict(
                db.execute(
                    select(
                        DemoChatMessage.role,
                        func.count(DemoChatMessage.id),
                    ).group_by(DemoChatMessage.role)
                ).all()
            )
            assistant_content = db.scalar(
                select(DemoChatMessage.content).where(
                    DemoChatMessage.role == "assistant"
                )
            )
            reservation_count = db.scalar(
                select(func.count()).select_from(Reservation)
            )
            handoff_count = db.scalar(
                select(func.count()).select_from(DemoHandoffEvent)
            )

        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.json()["code"], "RATE_LIMIT_EXCEEDED")
        self.assertEqual(rejected.headers["x-ratelimit-limit"], "20")
        self.assertEqual(rejected.headers["x-ratelimit-remaining"], "0")
        self.assertGreater(int(rejected.headers["retry-after"]), 0)
        self.assertEqual(
            rejected.headers["x-ratelimit-reset"],
            str(
                int(
                    datetime(
                        2026,
                        8,
                        2,
                        10,
                        1,
                        tzinfo=timezone.utc,
                    ).timestamp()
                )
            ),
        )
        self.assertEqual(rejected.headers["cache-control"], "no-store")
        self.assertEqual(
            rate_counts,
            {"ip": 21, "session": 21, "global": 21},
        )
        self.assertEqual(core_calls, ["Replay yang sama"])
        self.assertEqual(role_counts, {"assistant": 1, "user": 1})
        self.assertEqual(assistant_content, "Stored assistant reply.")
        self.assertEqual(reservation_count, 1)
        self.assertEqual(handoff_count, 1)
        self.assertNotIn(SESSION_TOKEN, rejected.text)
        self.assertNotIn(
            digest_demo_session_token(SESSION_TOKEN),
            rejected.text,
        )
        self.assertNotIn("chat", rejected.text.casefold())
        self.assertNotIn("sql", rejected.text.casefold())

    def test_second_policy_failure_rolls_back_and_suppresses_business(self):
        buckets = _FailSecondPolicyBuckets()
        failing_rate_limits = DemoRateLimitService(
            bucket_repository=buckets,
            session_service=self.sessions,
            clock=self.clock,
        )
        chat = SimpleNamespace(process=AsyncMock())
        self.app.dependency_overrides[get_demo_rate_limit_service] = (
            lambda: failing_rate_limits
        )
        self.app.dependency_overrides[get_demo_chat_service] = lambda: chat
        response = self.client.post(
            "/internal/demo/chat",
            headers=self.headers(),
            json={
                "message": "Tidak boleh diproses",
                "requestId": "bf22a65d-e311-4433-b4d6-6117dc07c274",
            },
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "SERVICE_UNAVAILABLE")
        self.assertEqual(buckets.scopes, ["ip", "session"])
        chat.process.assert_not_awaited()
        with self.Session() as db:
            for model in (
                DemoRateLimitBucket,
                DemoChatMessage,
                DemoHandoffEvent,
                Reservation,
            ):
                self.assertEqual(
                    db.scalar(select(func.count()).select_from(model)),
                    0,
                )
        rendered = response.text.casefold()
        self.assertNotIn(SESSION_TOKEN.casefold(), rendered)
        self.assertNotIn(
            digest_demo_session_token(SESSION_TOKEN),
            rendered,
        )
        self.assertNotIn("sql", rendered)
