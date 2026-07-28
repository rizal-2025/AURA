"""Focused offline tests for isolated demo persistence."""

from datetime import datetime, timedelta, timezone
import unittest

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.ownership import MissingOwnerCustomerError
from app.core.transaction_errors import PersistenceOperationError
from app.core.unit_of_work import UnitOfWork
from app.db.models.customer import Customer
from app.db.models.demo_persistence import (
    DemoChatMessage,
    DemoHandoffEvent,
    DemoRateLimitBucket,
    DemoSession,
)
from app.db.models.support_ticket import SupportTicket
from app.db.models.support_ticket_notification import (
    SupportTicketNotification,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
    DemoRateLimitBucketRepository,
    DemoSessionRepository,
)


class DemoPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.create_function("char_length", 1, len)

        Customer.__table__.create(self.engine)
        SupportTicket.__table__.create(self.engine)
        SupportTicketNotification.__table__.create(self.engine)
        DemoSession.__table__.create(self.engine)
        DemoChatMessage.__table__.create(self.engine)
        DemoHandoffEvent.__table__.create(self.engine)
        DemoRateLimitBucket.__table__.create(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
        )
        self.db = self.Session()
        self.sessions = DemoSessionRepository()
        self.messages = DemoChatMessageRepository()
        self.handoffs = DemoHandoffEventRepository()
        self.buckets = DemoRateLimitBucketRepository()
        self.now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def digest(seed: int) -> str:
        return f"{seed:064x}"

    def owner(self):
        owner = Customer()
        self.db.add(owner)
        self.db.commit()
        return owner

    def demo_session(
        self,
        seed: int,
        *,
        owner=None,
        now: datetime | None = None,
        idle_expires_at: datetime | None = None,
        absolute_expires_at: datetime | None = None,
    ):
        timestamp = now or self.now
        owner = owner or self.owner()
        row = self.sessions.create(
            self.db,
            token_digest=self.digest(seed),
            owner_customer_id=owner.id,
            now=timestamp,
            idle_expires_at=idle_expires_at
            or timestamp + timedelta(minutes=30),
            absolute_expires_at=absolute_expires_at
            or timestamp + timedelta(hours=2),
        )
        self.db.commit()
        return row, owner

    def bucket(
        self,
        seed: int,
        *,
        scope_type: str = "session",
        expires_at: datetime | None = None,
    ):
        row = self.buckets.create(
            self.db,
            scope_type=scope_type,
            subject_digest=self.digest(seed),
            action="chat.send",
            window_started_at=self.now,
            window_seconds=60,
            request_count=1,
            expires_at=expires_at or self.now + timedelta(minutes=1),
            now=self.now,
        )
        self.db.commit()
        return row

    def test_raw_token_and_raw_ip_have_no_persistence_columns(self):
        session_columns = set(DemoSession.__table__.columns.keys())
        bucket_columns = set(DemoRateLimitBucket.__table__.columns.keys())
        self.assertNotIn("token", session_columns)
        self.assertNotIn("raw_token", session_columns)
        self.assertIn("token_digest", session_columns)
        self.assertNotIn("ip", bucket_columns)
        self.assertNotIn("raw_ip", bucket_columns)
        self.assertIn("subject_digest", bucket_columns)

    def test_token_digest_is_required_and_validated(self):
        owner = self.owner()
        for value in (None, "", "raw-session-token", "g" * 64):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    self.sessions.create(
                        self.db,
                        token_digest=value,
                        owner_customer_id=owner.id,
                        idle_expires_at=self.now + timedelta(minutes=30),
                        absolute_expires_at=self.now + timedelta(hours=2),
                        now=self.now,
                    )

    def test_token_digest_is_unique(self):
        self.demo_session(1)
        owner = self.owner()
        with self.assertRaises(IntegrityError):
            self.sessions.create(
                self.db,
                token_digest=self.digest(1),
                owner_customer_id=owner.id,
                idle_expires_at=self.now + timedelta(minutes=30),
                absolute_expires_at=self.now + timedelta(hours=2),
                now=self.now,
            )
        self.db.rollback()

    def test_owner_customer_is_required(self):
        with self.assertRaises(MissingOwnerCustomerError):
            self.sessions.create(
                self.db,
                token_digest=self.digest(2),
                owner_customer_id=None,
                idle_expires_at=self.now + timedelta(minutes=30),
                absolute_expires_at=self.now + timedelta(hours=2),
                now=self.now,
            )

    def test_owner_customer_can_belong_to_only_one_demo_session(self):
        owner = self.owner()
        self.demo_session(3, owner=owner)
        with self.assertRaises(IntegrityError):
            self.sessions.create(
                self.db,
                token_digest=self.digest(4),
                owner_customer_id=owner.id,
                idle_expires_at=self.now + timedelta(minutes=30),
                absolute_expires_at=self.now + timedelta(hours=2),
                now=self.now,
            )
        self.db.rollback()

    def test_environment_scope_accepts_only_demo(self):
        with self.assertRaises(ValueError):
            DemoSession(
                token_digest=self.digest(5),
                owner_customer_id=self.owner().id,
                environment_scope="production",
                idle_expires_at=self.now + timedelta(minutes=30),
                absolute_expires_at=self.now + timedelta(hours=2),
            )

    def test_active_lookup_accepts_valid_session(self):
        session, _owner = self.demo_session(6)
        found = self.sessions.get_active_by_token_digest(
            self.db,
            token_digest=self.digest(6),
            now=self.now,
        )
        self.assertEqual(found.id, session.id)

    def test_active_lookup_rejects_revoked_session(self):
        session, _owner = self.demo_session(7)
        self.sessions.revoke(
            self.db,
            demo_session_id=session.id,
            now=self.now,
        )
        self.db.commit()
        self.assertIsNone(
            self.sessions.get_active_by_token_digest(
                self.db,
                token_digest=self.digest(7),
                now=self.now,
            )
        )

    def test_active_lookup_rejects_idle_expired_session(self):
        self.demo_session(
            8,
            now=self.now - timedelta(hours=1),
            idle_expires_at=self.now - timedelta(seconds=1),
            absolute_expires_at=self.now + timedelta(hours=1),
        )
        self.assertIsNone(
            self.sessions.get_active_by_token_digest(
                self.db,
                token_digest=self.digest(8),
                now=self.now,
            )
        )

    def test_active_lookup_rejects_absolute_expired_session(self):
        self.demo_session(
            9,
            now=self.now - timedelta(hours=2),
            idle_expires_at=self.now - timedelta(minutes=1),
            absolute_expires_at=self.now - timedelta(seconds=1),
        )
        self.assertIsNone(
            self.sessions.get_active_by_token_digest(
                self.db,
                token_digest=self.digest(9),
                now=self.now,
            )
        )

    def test_sessions_use_distinct_owner_customers(self):
        first, first_owner = self.demo_session(10)
        second, second_owner = self.demo_session(11)
        self.assertNotEqual(first.owner_customer_id, second.owner_customer_id)
        self.assertNotEqual(first_owner.id, second_owner.id)

    def test_expired_session_is_available_for_cleanup(self):
        expired, _owner = self.demo_session(
            12,
            now=self.now - timedelta(hours=2),
            idle_expires_at=self.now - timedelta(minutes=1),
            absolute_expires_at=self.now - timedelta(seconds=1),
        )
        active, _owner = self.demo_session(13)
        found = self.sessions.list_expired(self.db, now=self.now)
        self.assertEqual([row.id for row in found], [expired.id])
        self.assertNotIn(active.id, {row.id for row in found})

    def test_user_message_can_be_appended(self):
        session, _owner = self.demo_session(14)
        row = self.messages.append(
            self.db,
            demo_session_id=session.id,
            role="user",
            content="Halo",
            created_at=self.now,
        )
        self.db.commit()
        self.assertEqual(row.role, "user")
        self.assertEqual(row.content, "Halo")

    def test_assistant_message_can_be_appended(self):
        session, _owner = self.demo_session(15)
        row = self.messages.append(
            self.db,
            demo_session_id=session.id,
            role="assistant",
            content="Halo juga",
            created_at=self.now,
        )
        self.db.commit()
        self.assertEqual(row.role, "assistant")

    def test_system_and_unknown_message_roles_are_rejected(self):
        session, _owner = self.demo_session(16)
        for role in ("system", "tool", "developer"):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    self.messages.append(
                        self.db,
                        demo_session_id=session.id,
                        role=role,
                        content="not persisted",
                        created_at=self.now,
                    )

    def test_history_is_limited_to_latest_fifty(self):
        session, _owner = self.demo_session(17)
        for number in range(55):
            self.messages.append(
                self.db,
                demo_session_id=session.id,
                role="user",
                content=f"message-{number:02d}",
                created_at=self.now + timedelta(seconds=number),
            )
        self.db.commit()
        history = self.messages.list_latest(
            self.db,
            demo_session_id=session.id,
        )
        self.assertEqual(len(history), 50)
        self.assertEqual(history[0].content, "message-05")
        self.assertEqual(history[-1].content, "message-54")

    def test_history_is_returned_chronologically(self):
        session, _owner = self.demo_session(18)
        for number in (3, 1, 2):
            self.messages.append(
                self.db,
                demo_session_id=session.id,
                role="assistant",
                content=str(number),
                created_at=self.now + timedelta(seconds=number),
            )
        self.db.commit()
        history = self.messages.list_latest(
            self.db,
            demo_session_id=session.id,
        )
        self.assertEqual([row.content for row in history], ["1", "2", "3"])

    def test_message_history_is_session_scoped(self):
        first, _owner = self.demo_session(19)
        second, _owner = self.demo_session(20)
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
        self.db.commit()
        first_history = self.messages.list_latest(
            self.db,
            demo_session_id=first.id,
        )
        self.assertEqual([row.content for row in first_history], ["first-only"])

    def test_delete_messages_is_session_scoped(self):
        first, _owner = self.demo_session(21)
        second, _owner = self.demo_session(22)
        for session, content in ((first, "first"), (second, "second")):
            self.messages.append(
                self.db,
                demo_session_id=session.id,
                role="user",
                content=content,
                created_at=self.now,
            )
        self.messages.delete_by_demo_session(
            self.db,
            demo_session_id=first.id,
        )
        self.db.commit()
        self.assertEqual(
            [row.content for row in self.messages.list_latest(
                self.db,
                demo_session_id=second.id,
            )],
            ["second"],
        )

    def test_simulated_handoff_can_be_created_with_safe_summary(self):
        session, _owner = self.demo_session(23)
        row = self.handoffs.create_simulated(
            self.db,
            demo_session_id=session.id,
            reference="DEMO-HO-000001",
            reason_code="explicit_human_request",
            created_at=self.now,
        )
        self.db.commit()
        self.assertEqual(row.status, "simulated")
        self.assertNotIn("prompt", row.safe_summary.casefold())

    def test_handoff_reference_is_unique(self):
        first, _owner = self.demo_session(24)
        second, _owner = self.demo_session(25)
        self.handoffs.create_simulated(
            self.db,
            demo_session_id=first.id,
            reference="DEMO-HO-UNIQUE",
            reason_code="internal_error",
            created_at=self.now,
        )
        self.db.commit()
        with self.assertRaises(IntegrityError):
            self.handoffs.create_simulated(
                self.db,
                demo_session_id=second.id,
                reference="DEMO-HO-UNIQUE",
                reason_code="internal_error",
                created_at=self.now,
            )
        self.db.rollback()

    def test_handoff_status_other_than_simulated_is_rejected(self):
        with self.assertRaises(ValueError):
            DemoHandoffEvent(
                demo_session_id=1,
                reference="DEMO-HO-INVALID-STATUS",
                status="open",
                reason_code="internal_error",
                safe_summary=None,
                created_at=self.now,
            )

    def test_handoff_rejects_caller_supplied_summary(self):
        with self.assertRaises(ValueError):
            DemoHandoffEvent(
                demo_session_id=1,
                reference="DEMO-HO-RAW-SUMMARY",
                status="simulated",
                reason_code="internal_error",
                safe_summary="Raw visitor message must not be stored here.",
                created_at=self.now,
            )

    def test_handoff_events_are_session_scoped(self):
        first, _owner = self.demo_session(26)
        second, _owner = self.demo_session(27)
        self.handoffs.create_simulated(
            self.db,
            demo_session_id=first.id,
            reference="DEMO-HO-FIRST",
            reason_code="internal_error",
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
        found = self.handoffs.list_latest(
            self.db,
            demo_session_id=first.id,
        )
        self.assertEqual([row.reference for row in found], ["DEMO-HO-FIRST"])

    def test_simulated_handoff_creates_no_ticket_or_outbox(self):
        session, _owner = self.demo_session(28)
        self.handoffs.create_simulated(
            self.db,
            demo_session_id=session.id,
            reference="DEMO-HO-NO-OUTBOX",
            reason_code="repeated_misunderstanding",
            created_at=self.now,
        )
        self.db.commit()
        ticket_count = self.db.scalar(
            select(func.count()).select_from(SupportTicket)
        )
        outbox_count = self.db.scalar(
            select(func.count()).select_from(SupportTicketNotification)
        )
        self.assertEqual((ticket_count, outbox_count), (0, 0))

    def test_rate_limit_subject_is_a_digest(self):
        for value in ("127.0.0.1", "visitor@example.test", "short"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.buckets.create(
                        self.db,
                        scope_type="ip",
                        subject_digest=value,
                        action="chat.send",
                        window_started_at=self.now,
                        window_seconds=60,
                        request_count=0,
                        expires_at=self.now + timedelta(minutes=1),
                        now=self.now,
                    )

    def test_rate_limit_bucket_identity_is_unique(self):
        self.bucket(29)
        with self.assertRaises(IntegrityError):
            self.buckets.create(
                self.db,
                scope_type="session",
                subject_digest=self.digest(29),
                action="chat.send",
                window_started_at=self.now,
                window_seconds=60,
                request_count=2,
                expires_at=self.now + timedelta(minutes=1),
                now=self.now,
            )
        self.db.rollback()

    def test_rate_limit_request_count_cannot_be_negative(self):
        with self.assertRaises(ValueError):
            self.buckets.create(
                self.db,
                scope_type="session",
                subject_digest=self.digest(30),
                action="chat.send",
                window_started_at=self.now,
                window_seconds=60,
                request_count=-1,
                expires_at=self.now + timedelta(minutes=1),
                now=self.now,
            )

    def test_expired_rate_limit_bucket_is_available_for_cleanup(self):
        expired = self.bucket(
            31,
            expires_at=self.now - timedelta(seconds=1),
        )
        self.bucket(32)
        found = self.buckets.list_expired(self.db, now=self.now)
        self.assertEqual([row.id for row in found], [expired.id])

    def test_session_ip_and_global_bucket_scopes_are_distinct(self):
        rows = [
            self.bucket(33, scope_type=scope)
            for scope in ("session", "ip", "global")
        ]
        self.assertEqual(
            {row.scope_type for row in rows},
            {"session", "ip", "global"},
        )

    def test_delete_expired_buckets_preserves_active_bucket(self):
        expired = self.bucket(
            34,
            expires_at=self.now - timedelta(seconds=1),
        )
        active = self.bucket(35)
        expired_id = expired.id
        active_id = active.id
        self.assertEqual(self.buckets.delete_expired(self.db, now=self.now), 1)
        self.db.commit()
        remaining = set(
            self.db.scalars(select(DemoRateLimitBucket.id)).all()
        )
        self.assertNotIn(expired_id, remaining)
        self.assertIn(active_id, remaining)

    def test_rollback_leaves_no_partial_demo_data(self):
        owner = self.owner()
        with self.assertRaises(PersistenceOperationError):
            with UnitOfWork(self.db):
                session = self.sessions.create(
                    self.db,
                    token_digest=self.digest(36),
                    owner_customer_id=owner.id,
                    idle_expires_at=self.now + timedelta(minutes=30),
                    absolute_expires_at=self.now + timedelta(hours=2),
                    now=self.now,
                )
                self.messages.append(
                    self.db,
                    demo_session_id=session.id,
                    role="user",
                    content="must roll back",
                    created_at=self.now,
                )
                raise RuntimeError("forced rollback")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DemoSession)),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DemoChatMessage)),
            0,
        )

    def test_missing_session_foreign_key_fails_safely(self):
        with self.assertRaises(PersistenceOperationError) as raised:
            with UnitOfWork(self.db) as unit:
                self.messages.append(
                    self.db,
                    demo_session_id=999999,
                    role="user",
                    content="orphan",
                    created_at=self.now,
                )
                unit.commit()
        output = str(raised.exception) + repr(raised.exception)
        self.assertEqual(str(raised.exception), "PERSISTENCE_OPERATION_FAILED")
        self.assertNotIn("orphan", output)

    def test_duplicate_integrity_error_is_sanitized(self):
        self.demo_session(37)
        owner = self.owner()
        digest = self.digest(37)
        with self.assertRaises(PersistenceOperationError) as raised:
            with UnitOfWork(self.db) as unit:
                self.sessions.create(
                    self.db,
                    token_digest=digest,
                    owner_customer_id=owner.id,
                    idle_expires_at=self.now + timedelta(minutes=30),
                    absolute_expires_at=self.now + timedelta(hours=2),
                    now=self.now,
                )
                unit.commit()
        output = str(raised.exception) + repr(raised.exception)
        self.assertNotIn(digest, output)
        self.assertEqual(str(raised.exception), "PERSISTENCE_OPERATION_FAILED")

    def test_child_first_cleanup_does_not_touch_other_owner(self):
        first, first_owner = self.demo_session(38)
        second, second_owner = self.demo_session(39)
        for session, suffix in ((first, "FIRST"), (second, "SECOND")):
            self.messages.append(
                self.db,
                demo_session_id=session.id,
                role="user",
                content=suffix,
                created_at=self.now,
            )
            self.handoffs.create_simulated(
                self.db,
                demo_session_id=session.id,
                reference=f"DEMO-HO-{suffix}",
                reason_code="internal_error",
                created_at=self.now,
            )
        self.db.commit()

        self.messages.delete_by_demo_session(
            self.db,
            demo_session_id=first.id,
        )
        self.handoffs.delete_by_demo_session(
            self.db,
            demo_session_id=first.id,
        )
        self.sessions.delete_internal_by_id(
            self.db,
            demo_session_id=first.id,
        )
        self.db.commit()

        self.assertIsNone(self.db.get(DemoSession, first.id))
        self.assertIsNotNone(self.db.get(DemoSession, second.id))
        self.assertIsNotNone(self.db.get(Customer, first_owner.id))
        self.assertIsNotNone(self.db.get(Customer, second_owner.id))
        self.assertEqual(
            [row.content for row in self.messages.list_latest(
                self.db,
                demo_session_id=second.id,
            )],
            ["SECOND"],
        )


if __name__ == "__main__":
    unittest.main()
