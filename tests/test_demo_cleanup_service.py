"""Unit tests for bounded, owner-safe demo cleanup orchestration."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import unittest

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models.conversation_workflow_state import (
    ConversationWorkflowState,
)
from app.db.models.customer import Customer
from app.db.models.demo_persistence import (
    DemoChatMessage,
    DemoHandoffEvent,
    DemoRateLimitBucket,
    DemoSession,
)
from app.db.models.reservation import Reservation
from app.db.models.telegram_identity import TelegramIdentity
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
from app.db.repositories.conversation_workflow_state_repository import (
    ConversationWorkflowStateRepository,
)
from app.db.repositories.reservation_repository import ReservationRepository
from app.services.demo_chat_service import (
    DemoPostgreSQLAdvisoryLock,
    _DemoAdvisoryLockLease,
)
from app.services.demo_cleanup_service import (
    DemoCleanupConfigurationError,
    DemoCleanupService,
    validate_demo_cleanup_batch_size,
)
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)


class _Lease:
    pass


class _FakeAdvisoryLock:
    def __init__(self, locked_ids=()):
        self.locked_ids = set(locked_ids)

    def acquire(self, _db, *, demo_session_id):
        return None if demo_session_id in self.locked_ids else _Lease()

    def release(self, _db, *, demo_session_id, lease):
        if not isinstance(lease, _Lease):
            raise RuntimeError("invalid test lease")


class _FailAfterMessageDelete(DemoChatMessageRepository):
    def delete_by_demo_session(self, db, *, demo_session_id):
        super().delete_by_demo_session(
            db,
            demo_session_id=demo_session_id,
        )
        raise RuntimeError("forced cleanup failure")


class _FailAfterWorkflowDelete(ConversationWorkflowStateRepository):
    def delete_by_scope(self, db, **values):
        super().delete_by_scope(db, **values)
        raise RuntimeError("forced workflow cleanup failure")


class _FailAfterHandoffDelete(DemoHandoffEventRepository):
    def delete_by_demo_session(self, db, *, demo_session_id):
        super().delete_by_demo_session(
            db,
            demo_session_id=demo_session_id,
        )
        raise RuntimeError("forced handoff cleanup failure")


class _FailAfterReservationDelete(ReservationRepository):
    def delete_by_owner_customer_id(self, db, owner_customer_id):
        super().delete_by_owner_customer_id(db, owner_customer_id)
        raise RuntimeError("forced reservation cleanup failure")


class _FailAfterRateBucketDelete(DemoRateLimitBucketRepository):
    def delete_session_subject(self, db, *, subject_digest):
        super().delete_session_subject(
            db,
            subject_digest=subject_digest,
        )
        raise RuntimeError("forced rate-bucket cleanup failure")


class _FailAfterSessionDelete(DemoSessionRepository):
    def delete_internal_by_id(self, db, *, demo_session_id):
        super().delete_internal_by_id(
            db,
            demo_session_id=demo_session_id,
        )
        raise RuntimeError("forced session cleanup failure")


class _TrackedLockConnection:
    def __init__(self, release_result):
        self.release_result = release_result
        self.invalidations = 0
        self.closes = 0
        self.commits = 0

    def scalar(self, *_args, **_values):
        if isinstance(self.release_result, BaseException):
            raise self.release_result
        return self.release_result

    def invalidate(self):
        self.invalidations += 1

    def close(self):
        self.closes += 1

    def commit(self):
        self.commits += 1


class _UncertainReleaseAdvisoryLock(DemoPostgreSQLAdvisoryLock):
    def __init__(self, release_result):
        self.release_result = release_result
        self.connections = []

    def acquire(self, _db, *, demo_session_id):
        connection = _TrackedLockConnection(self.release_result)
        self.connections.append(connection)
        return _DemoAdvisoryLockLease(
            connection=connection,
            lock_key=self._key(demo_session_id),
        )


class _RevalidatedInactiveRepository(DemoSessionRepository):
    def get_expired_by_id_for_update(self, *_args, **_values):
        return None


class _CountingExpiredBucketRepository:
    def __init__(self):
        self.calls = []

    def delete_expired_batch(self, _db, *, now, limit):
        self.calls.append((now, limit))
        return limit


class DemoCleanupServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        def configure(connection, _record):
            connection.create_function("char_length", 1, len)
            connection.create_function("jsonb_typeof", 1, lambda _v: "object")

        event.listen(self.engine, "connect", configure)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.engine.dispose()

    def create_session(
        self,
        marker,
        *,
        idle_delta=timedelta(minutes=-1),
        absolute_delta=timedelta(hours=1),
        revoked=False,
    ):
        db = self.Session()
        owner = Customer()
        db.add(owner)
        db.flush()
        row = DemoSession(
            token_digest=hashlib.sha256(marker.encode()).hexdigest(),
            owner_customer_id=owner.id,
            environment_scope="demo",
            created_at=self.now - timedelta(hours=2),
            last_seen_at=self.now - timedelta(hours=1),
            idle_expires_at=self.now + idle_delta,
            absolute_expires_at=self.now + absolute_delta,
            revoked_at=self.now - timedelta(seconds=30) if revoked else None,
            updated_at=self.now,
        )
        db.add(row)
        db.commit()
        values = (row.id, owner.id, row.token_digest)
        db.close()
        return values

    def service(self, **values):
        values.setdefault("database_lock", _FakeAdvisoryLock())
        return DemoCleanupService(
            session_factory=self.Session,
            app_env="demo",
            clock=lambda: self.now,
            **values,
        )

    def run_cleanup(self, service=None, **values):
        return asyncio.run((service or self.service()).run_once(**values))

    def count(self, model):
        db = self.Session()
        try:
            return int(db.scalar(select(func.count()).select_from(model)) or 0)
        finally:
            db.close()

    def create_session_with_message(self, marker):
        values = self.create_session(marker)
        db = self.Session()
        db.add(
            DemoChatMessage(
                demo_session_id=values[0],
                role="user",
                content="must survive rollback",
                created_at=self.now,
            )
        )
        db.commit()
        db.close()
        return values

    def assert_cleanup_failure_rolls_back(self, service):
        summary = self.run_cleanup(service)
        self.assertEqual(summary.failed_sessions, 1)
        self.assertEqual(summary.cleaned_sessions, 0)
        self.assertEqual(self.count(DemoSession), 1)
        self.assertEqual(self.count(DemoChatMessage), 1)
        self.assertEqual(self.count(Customer), 1)

    def test_active_session_is_retained(self):
        self.create_session("active", idle_delta=timedelta(minutes=5))
        summary = self.run_cleanup()
        self.assertEqual(summary.scanned, 0)
        self.assertEqual(self.count(DemoSession), 1)
        self.assertEqual(self.count(Customer), 1)

    def test_dry_run_counts_exact_scope_and_performs_zero_mutations(self):
        expired_id, expired_owner, expired_digest = self.create_session(
            "dry-expired"
        )
        active_id, active_owner, _ = self.create_session(
            "dry-active",
            idle_delta=timedelta(minutes=5),
        )
        db = self.Session()
        db.add_all(
            [
                DemoChatMessage(
                    demo_session_id=expired_id,
                    role="user",
                    content="expired one",
                    created_at=self.now,
                ),
                DemoChatMessage(
                    demo_session_id=expired_id,
                    role="assistant",
                    content="expired two",
                    created_at=self.now,
                ),
                DemoChatMessage(
                    demo_session_id=active_id,
                    role="user",
                    content="active",
                    created_at=self.now,
                ),
                Reservation(
                    name="Pending",
                    people=2,
                    date="2026-08-04",
                    time="19:00",
                    owner_customer_id=expired_owner,
                    status="pending",
                ),
                Reservation(
                    name="Cancelled",
                    people=2,
                    date="2026-08-04",
                    time="20:00",
                    owner_customer_id=expired_owner,
                    status="cancelled",
                ),
                Reservation(
                    name="Active owner",
                    people=2,
                    date="2026-08-04",
                    time="21:00",
                    owner_customer_id=active_owner,
                    status="pending",
                ),
                ConversationWorkflowState(
                    owner_customer_id=expired_owner,
                    session_reference_hash=(
                        ConversationWorkflowStateService.hash_session_reference(
                            f"demo-session-{expired_id}"
                        )
                    ),
                    schema_version=1,
                    payload={},
                    is_active=True,
                    revision=1,
                    created_at=self.now,
                    updated_at=self.now,
                ),
                DemoHandoffEvent(
                    demo_session_id=expired_id,
                    reference="DEMO-HO-DRYRUN",
                    status="simulated",
                    reason_code="internal_error",
                    safe_summary=(
                        "The demo assistant could not safely complete the request."
                    ),
                    created_at=self.now,
                ),
                DemoRateLimitBucket(
                    scope_type="session",
                    subject_digest=expired_digest,
                    action="chat",
                    window_started_at=self.now - timedelta(minutes=1),
                    window_seconds=60,
                    request_count=1,
                    expires_at=self.now + timedelta(minutes=1),
                    updated_at=self.now,
                ),
                DemoRateLimitBucket(
                    scope_type="global",
                    subject_digest=hashlib.sha256(b"dry-global").hexdigest(),
                    action="chat",
                    window_started_at=self.now - timedelta(minutes=2),
                    window_seconds=60,
                    request_count=1,
                    expires_at=self.now,
                    updated_at=self.now,
                ),
            ]
        )
        db.commit()
        db.close()

        mutation_statements = []

        def capture_mutation(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            if statement.lstrip().upper().startswith(("DELETE", "UPDATE")):
                mutation_statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture_mutation)
        try:
            first = self.service().dry_run_once()
            second = self.service().dry_run_once()
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                capture_mutation,
            )

        self.assertEqual(first, second)
        self.assertEqual(first.eligible_sessions, 1)
        self.assertEqual(first.eligible_messages, 2)
        self.assertEqual(first.eligible_reservations, 2)
        self.assertEqual(first.eligible_workflow_states, 1)
        self.assertEqual(first.eligible_handoffs, 1)
        self.assertEqual(first.eligible_session_buckets, 1)
        self.assertEqual(first.eligible_expired_buckets, 1)
        self.assertEqual(first.blocked_sessions, 0)
        self.assertEqual(mutation_statements, [])
        self.assertEqual(self.count(DemoSession), 2)
        self.assertEqual(self.count(DemoChatMessage), 3)
        self.assertEqual(self.count(Reservation), 3)

    def test_idle_expired_session_and_owner_are_cleaned(self):
        self.create_session("idle")
        summary = self.run_cleanup()
        self.assertEqual(summary.cleaned_sessions, 1)
        self.assertEqual(self.count(DemoSession), 0)
        self.assertEqual(self.count(Customer), 0)

    def test_absolute_expired_and_boundary_sessions_are_eligible(self):
        self.create_session(
            "absolute",
            idle_delta=timedelta(seconds=-2),
            absolute_delta=timedelta(seconds=-1),
        )
        self.create_session(
            "boundary",
            idle_delta=timedelta(0),
            absolute_delta=timedelta(hours=1),
        )
        summary = self.run_cleanup()
        self.assertEqual(summary.cleaned_sessions, 2)

    def test_revoked_session_is_cleaned(self):
        self.create_session(
            "revoked",
            idle_delta=timedelta(hours=1),
            revoked=True,
        )
        self.assertEqual(self.run_cleanup().cleaned_sessions, 1)

    def test_batch_limit_and_deterministic_order_apply(self):
        first = self.create_session("first", idle_delta=timedelta(minutes=-3))
        second = self.create_session("second", idle_delta=timedelta(minutes=-2))
        self.create_session("third", idle_delta=timedelta(minutes=-1))
        summary = self.run_cleanup(batch_size=2)
        self.assertEqual(summary.scanned, 2)
        db = self.Session()
        try:
            remaining = set(db.scalars(select(DemoSession.id)))
        finally:
            db.close()
        self.assertNotIn(first[0], remaining)
        self.assertNotIn(second[0], remaining)
        self.assertEqual(len(remaining), 1)

    def test_cleanup_is_idempotent(self):
        self.create_session("once")
        self.assertEqual(self.run_cleanup().cleaned_sessions, 1)
        second = self.run_cleanup()
        self.assertEqual(second.cleaned_sessions, 0)
        self.assertEqual(second.scanned, 0)

    def test_children_and_session_bucket_are_deleted_child_first(self):
        session_id, owner_id, digest = self.create_session("children")
        db = self.Session()
        workflow_hash = (
            ConversationWorkflowStateService.hash_session_reference(
                f"demo-session-{session_id}"
            )
        )
        db.add_all(
            [
                DemoChatMessage(
                    demo_session_id=session_id,
                    role="user",
                    content="incomplete marker",
                    created_at=self.now,
                ),
                DemoHandoffEvent(
                    demo_session_id=session_id,
                    reference="DEMO-HO-AB12CD34",
                    status="simulated",
                    reason_code="internal_error",
                    safe_summary=(
                        "The demo assistant could not safely complete the request."
                    ),
                    created_at=self.now,
                ),
                ConversationWorkflowState(
                    owner_customer_id=owner_id,
                    session_reference_hash=workflow_hash,
                    schema_version=1,
                    payload={},
                    is_active=True,
                    revision=1,
                    created_at=self.now,
                    updated_at=self.now,
                ),
                Reservation(
                    name="Demo",
                    people=2,
                    date="2026-08-04",
                    time="19:00",
                    owner_customer_id=owner_id,
                ),
                Reservation(
                    name="Cancelled demo",
                    people=2,
                    date="2026-08-04",
                    time="20:00",
                    owner_customer_id=owner_id,
                    status="cancelled",
                ),
                DemoRateLimitBucket(
                    scope_type="session",
                    subject_digest=digest,
                    action="chat",
                    window_started_at=self.now - timedelta(minutes=2),
                    window_seconds=60,
                    request_count=3,
                    expires_at=self.now + timedelta(minutes=1),
                    updated_at=self.now,
                ),
            ]
        )
        db.commit()
        db.close()
        self.assertEqual(self.run_cleanup().cleaned_sessions, 1)
        for model in (
            DemoChatMessage,
            DemoHandoffEvent,
            ConversationWorkflowState,
            Reservation,
            DemoSession,
            Customer,
        ):
            self.assertEqual(self.count(model), 0)
        self.assertEqual(self.count(DemoRateLimitBucket), 0)

    def test_expired_all_scope_buckets_deleted_active_buckets_retained(self):
        db = self.Session()
        for index, scope in enumerate(("session", "global", "ip")):
            db.add(
                DemoRateLimitBucket(
                    scope_type=scope,
                    subject_digest=hashlib.sha256(
                        f"{scope}-{index}".encode()
                    ).hexdigest(),
                    action="chat",
                    window_started_at=self.now - timedelta(minutes=2),
                    window_seconds=60,
                    request_count=2,
                    expires_at=(
                        self.now
                        if index < 2
                        else self.now + timedelta(minutes=1)
                    ),
                    updated_at=self.now,
                )
            )
        db.commit()
        db.close()
        first = self.run_cleanup(batch_size=1)
        second = self.run_cleanup(batch_size=1)
        third = self.run_cleanup(batch_size=1)
        self.assertEqual(first.deleted_expired_buckets, 1)
        self.assertEqual(second.deleted_expired_buckets, 1)
        self.assertEqual(third.deleted_expired_buckets, 0)
        self.assertEqual(self.count(DemoRateLimitBucket), 1)

    def test_expired_bucket_repository_is_called_only_once_per_run(self):
        buckets = _CountingExpiredBucketRepository()
        summary = self.run_cleanup(
            self.service(rate_bucket_repository=buckets),
            batch_size=7,
        )
        self.assertEqual(buckets.calls, [(self.now, 7)])
        self.assertEqual(summary.deleted_expired_buckets, 7)

    def test_lock_conflict_skips_without_partial_delete(self):
        session_id, _, _ = self.create_session("locked")
        service = self.service(
            database_lock=_FakeAdvisoryLock({session_id})
        )
        summary = self.run_cleanup(service)
        self.assertEqual(summary.skipped_locked, 1)
        self.assertEqual(self.count(DemoSession), 1)

    def test_eligibility_is_revalidated_after_lock(self):
        self.create_session("changed")
        service = self.service(
            session_repository=_RevalidatedInactiveRepository()
        )
        summary = self.run_cleanup(service)
        self.assertEqual(summary.skipped_not_eligible, 1)
        self.assertEqual(self.count(DemoSession), 1)

    def test_one_failure_rolls_back_and_next_session_continues(self):
        first = self.create_session(
            "unsafe",
            idle_delta=timedelta(minutes=-2),
        )
        self.create_session("safe", idle_delta=timedelta(minutes=-1))
        db = self.Session()
        db.add(
            TelegramIdentity(
                telegram_user_key="f" * 64,
                customer_id=first[1],
                is_active=True,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        db.commit()
        db.close()
        summary = self.run_cleanup()
        self.assertEqual(summary.failed_sessions, 1)
        self.assertEqual(summary.cleaned_sessions, 1)
        self.assertEqual(self.count(TelegramIdentity), 1)
        self.assertEqual(self.count(DemoSession), 1)
        self.assertEqual(self.count(Customer), 1)

    def test_transaction_failure_leaves_no_partial_cleanup(self):
        self.create_session_with_message("rollback")
        service = self.service(
            message_repository=_FailAfterMessageDelete()
        )
        self.assert_cleanup_failure_rolls_back(service)

    def test_workflow_delete_failure_rolls_back_entire_session(self):
        self.create_session_with_message("workflow-rollback")
        self.assert_cleanup_failure_rolls_back(
            self.service(
                workflow_repository=_FailAfterWorkflowDelete()
            )
        )

    def test_handoff_delete_failure_rolls_back_entire_session(self):
        self.create_session_with_message("handoff-rollback")
        self.assert_cleanup_failure_rolls_back(
            self.service(
                handoff_repository=_FailAfterHandoffDelete()
            )
        )

    def test_reservation_delete_failure_rolls_back_entire_session(self):
        self.create_session_with_message("reservation-rollback")
        self.assert_cleanup_failure_rolls_back(
            self.service(
                reservation_repository=_FailAfterReservationDelete()
            )
        )

    def test_rate_bucket_delete_failure_rolls_back_entire_session(self):
        self.create_session_with_message("bucket-rollback")
        self.assert_cleanup_failure_rolls_back(
            self.service(
                rate_bucket_repository=_FailAfterRateBucketDelete()
            )
        )

    def test_demo_session_delete_failure_rolls_back_entire_session(self):
        self.create_session_with_message("session-rollback")
        self.assert_cleanup_failure_rolls_back(
            self.service(
                session_repository=_FailAfterSessionDelete()
            )
        )

    def test_customer_delete_failure_rolls_back_entire_session(self):
        self.create_session_with_message("customer-rollback")

        def fail_customer_delete(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            if statement.lstrip().upper().startswith(
                "DELETE FROM CUSTOMERS"
            ):
                raise RuntimeError("forced customer cleanup failure")

        event.listen(
            self.engine,
            "before_cursor_execute",
            fail_customer_delete,
        )
        try:
            self.assert_cleanup_failure_rolls_back(self.service())
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                fail_customer_delete,
            )

    def assert_uncertain_unlock_preserves_committed_cleanup(
        self,
        release_result,
    ):
        self.create_session_with_message("unlock-first")
        self.create_session_with_message("unlock-second")
        database_lock = _UncertainReleaseAdvisoryLock(release_result)
        service = self.service(database_lock=database_lock)
        first = self.run_cleanup(service)
        second = self.run_cleanup()
        self.assertEqual(first.scanned, 2)
        self.assertEqual(first.cleaned_sessions, 2)
        self.assertEqual(first.failed_sessions, 0)
        self.assertEqual(second.scanned, 0)
        self.assertEqual(second.cleaned_sessions, 0)
        self.assertEqual(second.failed_sessions, 0)
        self.assertEqual(self.count(DemoSession), 0)
        self.assertEqual(self.count(DemoChatMessage), 0)
        self.assertEqual(self.count(Customer), 0)
        self.assertEqual(len(database_lock.connections), 2)
        for connection in database_lock.connections:
            self.assertEqual(connection.invalidations, 1)
            self.assertEqual(connection.closes, 1)
            self.assertEqual(connection.commits, 0)
        rendered = repr(asdict(first))
        self.assertNotIn("unlock-first", rendered)
        self.assertNotIn("unlock-second", rendered)

    def test_advisory_unlock_false_preserves_committed_cleanup(self):
        self.assert_uncertain_unlock_preserves_committed_cleanup(False)

    def test_advisory_unlock_exception_preserves_committed_cleanup(self):
        self.assert_uncertain_unlock_preserves_committed_cleanup(
            RuntimeError("unsafe unlock detail")
        )

    def test_unknown_commit_outcome_is_safe_and_retry_is_idempotent(self):
        self.create_session_with_message("commit-outcome")
        factory_calls = 0

        def session_factory():
            nonlocal factory_calls
            factory_calls += 1
            db = self.Session()
            if factory_calls == 2:
                real_commit = db.commit

                def commit_then_disconnect():
                    real_commit()
                    raise RuntimeError("simulated disconnect after commit")

                db.commit = commit_then_disconnect
            return db

        service = DemoCleanupService(
            session_factory=session_factory,
            app_env="demo",
            database_lock=_FakeAdvisoryLock(),
            clock=lambda: self.now,
        )
        first = self.run_cleanup(service)
        second = self.run_cleanup()
        self.assertEqual(first.failed_sessions, 0)
        self.assertEqual(first.cleaned_sessions, 1)
        self.assertEqual(second.scanned, 0)
        self.assertEqual(self.count(DemoSession), 0)
        self.assertEqual(self.count(DemoChatMessage), 0)
        self.assertEqual(self.count(Customer), 0)

    def test_ticket_and_notification_block_entire_session_cleanup(self):
        session_id, owner_id, _ = self.create_session("ticketed")
        db = self.Session()
        ticket = SupportTicket(
            ticket_number="TKT-2026-DEMO-SAFE",
            owner_customer_id=owner_id,
            session_reference_hash="8" * 64,
            category="explicit_human_request",
            reason_code="explicit_human_request",
            priority="medium",
            safe_summary="Customer requested human assistance.",
            status="open",
            attempt_count=1,
            created_at=self.now,
            updated_at=self.now,
        )
        db.add(ticket)
        db.flush()
        db.add(
            SupportTicketNotification(
                support_ticket_id=ticket.id,
                channel="telegram_owner",
                status="pending",
                attempt_count=0,
                next_attempt_at=self.now,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        db.commit()
        db.close()
        summary = self.run_cleanup()
        self.assertEqual(summary.failed_sessions, 1)
        self.assertEqual(summary.cleaned_sessions, 0)
        self.assertEqual(self.count(SupportTicket), 1)
        self.assertEqual(self.count(SupportTicketNotification), 1)
        self.assertEqual(self.count(DemoSession), 1)
        self.assertEqual(self.count(Customer), 1)

    def test_non_demo_guard_precedes_factory_or_query(self):
        called = []

        def forbidden_factory():
            called.append(True)
            raise AssertionError("must not query")

        service = DemoCleanupService(
            session_factory=forbidden_factory,
            app_env="production",
            database_lock=_FakeAdvisoryLock(),
        )
        with self.assertRaises(DemoCleanupConfigurationError):
            self.run_cleanup(service)
        with self.assertRaises(DemoCleanupConfigurationError):
            service.dry_run_once()
        self.assertEqual(called, [])

    def test_batch_size_validation(self):
        for value in (0, 501, True, "100"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_demo_cleanup_batch_size(value)
        self.assertEqual(validate_demo_cleanup_batch_size(100), 100)
