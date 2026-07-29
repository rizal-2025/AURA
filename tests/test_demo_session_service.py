"""Offline unit tests for the internal demo-session lifecycle service."""

from base64 import urlsafe_b64decode
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import logging
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.transaction_errors import PersistenceOperationError
from app.db.models.customer import Customer
from app.db.models.demo_persistence import (
    DemoChatMessage,
    DemoHandoffEvent,
    DemoSession,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
    DemoSessionRepository,
)
from app.services.demo_session_service import (
    DEMO_SESSION_ABSOLUTE_TIMEOUT,
    DEMO_SESSION_IDLE_TIMEOUT,
    DemoSessionRequiredError,
    DemoSessionService,
    digest_demo_session_token,
    generate_demo_session_token,
)


TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
TOKEN_C = "C" * 43


class _FailingSessionRepository(DemoSessionRepository):
    def create(self, db, **values):
        raise RuntimeError("unsafe raw database marker")


class DemoSessionTokenTests(unittest.TestCase):
    def test_generator_contains_at_least_256_bits_of_random_material(self):
        raw_token = generate_demo_session_token()
        padding = "=" * (-len(raw_token) % 4)
        decoded = urlsafe_b64decode(raw_token + padding)
        self.assertEqual(len(decoded), 32)
        self.assertGreaterEqual(len(decoded) * 8, 256)

    def test_generator_returns_distinct_urlsafe_tokens(self):
        tokens = {generate_demo_session_token() for _ in range(16)}
        self.assertEqual(len(tokens), 16)
        self.assertTrue(all(len(token) >= 43 for token in tokens))

    def test_digest_is_deterministic_lowercase_sha256_hex(self):
        first = digest_demo_session_token(TOKEN_A)
        second = digest_demo_session_token(TOKEN_A)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_invalid_token_error_does_not_disclose_raw_value(self):
        raw_token = "unsafe raw session token"
        with self.assertRaises(DemoSessionRequiredError) as captured:
            digest_demo_session_token(raw_token)
        rendered = str(captured.exception) + repr(captured.exception)
        self.assertNotIn(raw_token, rendered)


class DemoSessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.create_function("char_length", 1, len)

        Customer.__table__.create(self.engine)
        DemoSession.__table__.create(self.engine)
        DemoChatMessage.__table__.create(self.engine)
        DemoHandoffEvent.__table__.create(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.now = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)
        self.sessions = DemoSessionRepository()
        self.messages = DemoChatMessageRepository()
        self.handoffs = DemoHandoffEventRepository()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(self, token=TOKEN_A, now=None, session_repository=None):
        return DemoSessionService(
            token_generator=lambda: token,
            clock=lambda: now or self.now,
            session_repository=session_repository,
        )

    def create(self, token=TOKEN_A, now=None):
        result = self.service(token=token, now=now).create_session(self.db)
        row = self.db.scalar(
            select(DemoSession).where(
                DemoSession.token_digest
                == digest_demo_session_token(token)
            )
        )
        return result, row

    def seed_session(
        self,
        token=TOKEN_A,
        *,
        now=None,
        idle=None,
        absolute=None,
    ):
        timestamp = now or self.now
        owner = Customer()
        self.db.add(owner)
        self.db.flush()
        row = self.sessions.create(
            self.db,
            token_digest=digest_demo_session_token(token),
            owner_customer_id=owner.id,
            now=timestamp,
            idle_expires_at=idle
            or timestamp + DEMO_SESSION_IDLE_TIMEOUT,
            absolute_expires_at=absolute
            or timestamp + DEMO_SESSION_ABSOLUTE_TIMEOUT,
        )
        self.db.commit()
        return row, owner

    def test_create_persists_only_digest_and_safe_response(self):
        response, row = self.create()
        self.assertEqual(
            row.token_digest,
            digest_demo_session_token(TOKEN_A),
        )
        self.assertNotEqual(row.token_digest, TOKEN_A)
        payload = response.model_dump(
            mode="json",
            by_alias=True,
        )
        self.assertEqual(payload["sessionToken"], TOKEN_A)
        rendered = repr(payload)
        for forbidden in (
            "owner",
            "customer",
            "token_digest",
            "session_id",
            "jwt",
        ):
            self.assertNotIn(forbidden, rendered.casefold())

    def test_create_is_atomic_and_uses_customer_defaults(self):
        self.create()
        customers = self.db.scalars(select(Customer)).all()
        sessions = self.db.scalars(select(DemoSession)).all()
        self.assertEqual(len(customers), 1)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].owner_customer_id, customers[0].id)
        self.assertTrue(customers[0].is_active)
        self.assertEqual(customers[0].token_version, 1)

    def test_two_creates_use_different_customers_and_tokens(self):
        first, first_row = self.create(TOKEN_A)
        second, second_row = self.create(TOKEN_B)
        self.assertNotEqual(
            first.session_token,
            second.session_token,
        )
        self.assertNotEqual(
            first_row.owner_customer_id,
            second_row.owner_customer_id,
        )

    def test_session_creation_failure_rolls_back_customer(self):
        service = self.service(
            session_repository=_FailingSessionRepository()
        )
        with self.assertRaises(PersistenceOperationError) as captured:
            service.create_session(self.db)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(Customer)),
            0,
        )
        rendered = str(captured.exception) + repr(captured.exception)
        self.assertNotIn(TOKEN_A, rendered)
        self.assertNotIn("unsafe raw database marker", rendered)

    def test_digest_collision_fails_safely_and_rolls_back_new_customer(self):
        self.create(TOKEN_A)
        with self.assertRaises(PersistenceOperationError) as captured:
            self.create(TOKEN_A)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(Customer)),
            1,
        )
        self.assertNotIn(
            TOKEN_A,
            str(captured.exception) + repr(captured.exception),
        )

    def test_create_does_not_issue_guest_jwt(self):
        with patch(
            "app.core.security.create_customer_access_token"
        ) as create_jwt:
            self.create()
        create_jwt.assert_not_called()

    def test_create_sets_utc_lifecycle_boundaries(self):
        response, row = self.create()
        self.assertEqual(
            row.created_at.replace(tzinfo=timezone.utc),
            self.now,
        )
        self.assertEqual(
            row.last_seen_at.replace(tzinfo=timezone.utc),
            self.now,
        )
        self.assertEqual(
            row.idle_expires_at.replace(tzinfo=timezone.utc),
            self.now + timedelta(hours=2),
        )
        self.assertEqual(
            row.absolute_expires_at.replace(tzinfo=timezone.utc),
            self.now + timedelta(hours=24),
        )
        self.assertIsNone(row.revoked_at)
        self.assertEqual(row.environment_scope, "demo")
        self.assertIsNotNone(response.session.expires_at.utcoffset())

    def test_valid_session_resolves_and_random_token_does_not(self):
        row, _owner = self.seed_session()
        service = self.service()
        self.assertEqual(
            service.resolve_active_session(self.db, TOKEN_A).id,
            row.id,
        )
        self.assertIsNone(
            service.resolve_active_session(self.db, TOKEN_B)
        )

    def test_revoked_idle_and_absolute_expired_sessions_are_rejected(self):
        revoked, _owner = self.seed_session(TOKEN_A)
        self.sessions.revoke(
            self.db,
            demo_session_id=revoked.id,
            now=self.now,
        )
        self.db.commit()
        self.seed_session(
            TOKEN_B,
            now=self.now - timedelta(hours=3),
            idle=self.now,
            absolute=self.now + timedelta(hours=1),
        )
        self.seed_session(
            TOKEN_C,
            now=self.now - timedelta(hours=25),
            idle=self.now,
            absolute=self.now,
        )
        service = self.service()
        for token in (TOKEN_A, TOKEN_B, TOKEN_C):
            with self.subTest(token=token[0]):
                self.assertIsNone(
                    service.resolve_active_session(self.db, token)
                )

    def test_non_demo_scope_is_rejected_by_active_resolution(self):
        row, _owner = self.seed_session()
        self.db.execute(text("PRAGMA ignore_check_constraints=ON"))
        self.db.execute(
            text(
                "UPDATE demo_sessions "
                "SET environment_scope='production' WHERE id=:id"
            ),
            {"id": row.id},
        )
        self.db.commit()
        self.db.execute(text("PRAGMA ignore_check_constraints=OFF"))
        self.db.expire_all()
        self.assertIsNone(
            self.service().resolve_active_session(self.db, TOKEN_A)
        )

    def test_current_touches_session_without_changing_absolute_expiry(self):
        row, _owner = self.seed_session(
            now=self.now - timedelta(minutes=30),
            idle=self.now + timedelta(minutes=15),
            absolute=self.now + timedelta(hours=5),
        )
        original_absolute = row.absolute_expires_at
        result = self.service().get_current_session(self.db, TOKEN_A)
        self.assertEqual(row.last_seen_at, self.now)
        self.assertEqual(
            row.idle_expires_at,
            self.now + timedelta(hours=2),
        )
        self.assertEqual(row.absolute_expires_at, original_absolute)
        self.assertEqual(
            result.session.idle_expires_at,
            row.idle_expires_at,
        )

    def test_touch_caps_idle_at_absolute_expiry(self):
        absolute = self.now + timedelta(minutes=30)
        row, _owner = self.seed_session(
            now=self.now - timedelta(minutes=5),
            idle=self.now + timedelta(minutes=10),
            absolute=absolute,
        )
        self.service().get_current_session(self.db, TOKEN_A)
        self.assertEqual(row.idle_expires_at, absolute)
        self.assertEqual(row.absolute_expires_at, absolute)

    def test_expired_session_is_not_revived_or_touched(self):
        old_seen = self.now - timedelta(hours=3)
        row, _owner = self.seed_session(
            now=old_seen,
            idle=self.now,
            absolute=self.now + timedelta(hours=1),
        )
        with self.assertRaises(DemoSessionRequiredError):
            self.service().get_current_session(self.db, TOKEN_A)
        self.assertEqual(row.last_seen_at, old_seen)
        self.assertEqual(row.idle_expires_at, self.now)

    def test_current_returns_latest_fifty_chronologically_and_true_count(self):
        row, _owner = self.seed_session()
        for number in range(55):
            self.messages.append(
                self.db,
                demo_session_id=row.id,
                role="user" if number % 2 == 0 else "assistant",
                content=f"message-{number:02d}",
                created_at=self.now + timedelta(seconds=number),
            )
        self.messages.append_request_message(
            self.db,
            demo_session_id=row.id,
            role="user",
            content="internal-incomplete-marker",
            request_id=uuid4(),
            created_at=self.now + timedelta(seconds=60),
        )
        self.db.commit()
        result = self.service(
            now=self.now + timedelta(minutes=1)
        ).get_current_session(self.db, TOKEN_A)
        self.assertEqual(len(result.messages), 50)
        self.assertEqual(result.messages[0].content, "message-05")
        self.assertEqual(result.messages[-1].content, "message-54")
        self.assertEqual(result.session.message_count, 55)

    def test_current_hides_incomplete_marker_but_keeps_legacy_and_pair(self):
        row, _owner = self.seed_session()
        self.messages.append(
            self.db,
            demo_session_id=row.id,
            role="user",
            content="legacy",
            created_at=self.now,
        )
        completed_request = uuid4()
        self.messages.append_request_message(
            self.db,
            demo_session_id=row.id,
            role="user",
            content="completed-user",
            request_id=completed_request,
            created_at=self.now + timedelta(seconds=1),
        )
        self.messages.append_request_message(
            self.db,
            demo_session_id=row.id,
            role="assistant",
            content="completed-assistant",
            request_id=completed_request,
            created_at=self.now + timedelta(seconds=2),
        )
        self.messages.append_request_message(
            self.db,
            demo_session_id=row.id,
            role="user",
            content="internal-incomplete-marker",
            request_id=uuid4(),
            created_at=self.now + timedelta(seconds=3),
        )
        self.db.commit()

        result = self.service().get_current_session(self.db, TOKEN_A)
        self.assertEqual(
            [message.content for message in result.messages],
            ["legacy", "completed-user", "completed-assistant"],
        )
        self.assertEqual(result.session.message_count, 3)

    def test_incomplete_marker_in_one_session_does_not_affect_another(self):
        first, _owner = self.seed_session(TOKEN_A)
        second, _owner = self.seed_session(TOKEN_B)
        self.messages.append_request_message(
            self.db,
            demo_session_id=first.id,
            role="user",
            content="first-incomplete",
            request_id=uuid4(),
            created_at=self.now,
        )
        self.messages.append(
            self.db,
            demo_session_id=second.id,
            role="user",
            content="second-public",
            created_at=self.now,
        )
        self.db.commit()

        result = self.service().get_current_session(self.db, TOKEN_B)
        self.assertEqual(
            [message.content for message in result.messages],
            ["second-public"],
        )
        self.assertEqual(result.session.message_count, 1)

    def test_current_messages_and_handoff_are_session_scoped(self):
        first, _owner = self.seed_session(TOKEN_A)
        second, _owner = self.seed_session(TOKEN_B)
        self.messages.append(
            self.db,
            demo_session_id=first.id,
            role="user",
            content="first-only",
            created_at=self.now,
        )
        self.messages.append(
            self.db,
            demo_session_id=second.id,
            role="user",
            content="second-only",
            created_at=self.now,
        )
        self.handoffs.create_simulated(
            self.db,
            demo_session_id=second.id,
            reference="DEMO-HO-SECOND",
            reason_code="internal_error",
            created_at=self.now,
        )
        self.db.commit()
        result = self.service().get_current_session(self.db, TOKEN_A)
        self.assertEqual(
            [message.content for message in result.messages],
            ["first-only"],
        )
        self.assertIsNone(result.handoff)

    def test_current_returns_latest_simulated_handoff(self):
        row, _owner = self.seed_session()
        self.handoffs.create_simulated(
            self.db,
            demo_session_id=row.id,
            reference="DEMO-HO-FIRST",
            reason_code="internal_error",
            created_at=self.now - timedelta(minutes=2),
        )
        latest = self.handoffs.create_simulated(
            self.db,
            demo_session_id=row.id,
            reference="DEMO-HO-LATEST",
            reason_code="explicit_human_request",
            created_at=self.now - timedelta(minutes=1),
        )
        self.db.commit()
        result = self.service().get_current_session(self.db, TOKEN_A)
        self.assertEqual(result.handoff.reference, latest.reference)
        self.assertEqual(result.handoff.status, "simulated")

    def test_empty_current_has_empty_messages_null_handoff_and_safe_fields(self):
        self.seed_session()
        result = self.service().get_current_session(self.db, TOKEN_A)
        payload = result.model_dump(mode="json", by_alias=True)
        self.assertEqual(payload["messages"], [])
        self.assertIsNone(payload["handoff"])
        rendered = repr(payload).casefold()
        for forbidden in (
            "demo_session_id",
            "owner",
            "customer",
            "token_digest",
            "sessiontoken",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_raw_token_is_not_emitted_to_logs(self):
        with self.assertLogs(level=logging.INFO) as captured:
            logging.getLogger().info("safe demo lifecycle marker")
            self.create()
        self.assertNotIn(TOKEN_A, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
