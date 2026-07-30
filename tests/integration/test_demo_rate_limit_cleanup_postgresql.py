"""Real PostgreSQL atomicity and lock safety for demo operational protection."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import os
import threading
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

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
from app.db.models.support_ticket import SupportTicket
from app.db.models.support_ticket_notification import (
    SupportTicketNotification,
)
from app.db.models.telegram_identity import TelegramIdentity
from app.core.conversation_lock_manager import ConversationLockManager
from app.core.unit_of_work import UnitOfWork
from app.db.repositories.demo_persistence_repository import (
    DemoHandoffEventRepository,
    DemoRateLimitBucketRepository,
    DemoSessionRepository,
    demo_session_rate_limit_subject,
)
from app.services.demo_chat_service import DemoPostgreSQLAdvisoryLock
from app.services.demo_cleanup_service import DemoCleanupService
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from app.services.demo_rate_limit_service import (
    DemoRateLimitAction,
    DemoRateLimitExceededError,
    DemoRateLimitService,
)
from app.services.demo_session_service import (
    DemoSessionService,
    digest_demo_session_token,
)
from migrations.add_demo_chat_request_id import migrate as migrate_request_id
from migrations.add_demo_persistence import migrate as migrate_demo
from tests.integration.disposable_schema import DisposableSchemaResources


TOKEN_A = "A" * 43
TOKEN_B = "B" * 43


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return "TEST_DATABASE_URL is not configured."
    try:
        parsed = make_url(value)
        if (
            parsed.get_backend_name() != "postgresql"
            or parsed.database != "aura_test"
        ):
            return "TEST_DATABASE_URL must target PostgreSQL aura_test."
    except Exception:
        return "TEST_DATABASE_URL is invalid."
    return None


SKIP_REASON = _skip_reason()


class _BarrierScanSessionRepository(DemoSessionRepository):
    def __init__(self, barrier):
        self.barrier = barrier

    def list_expired(self, db, *, now=None, limit=100):
        rows = super().list_expired(db, now=now, limit=limit)
        self.barrier.wait(timeout=10)
        return rows


class _AdvisoryRaceCoordinator:
    def __init__(self):
        self.attempt_barrier = threading.Barrier(2)
        self.loser_attempted = threading.Event()
        self.guard = threading.Lock()
        self.backend_pids = set()


class _CoordinatedAdvisoryLock:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.delegate = DemoPostgreSQLAdvisoryLock()

    def acquire(self, db, *, demo_session_id):
        backend_pid = int(db.scalar(text("SELECT pg_backend_pid()")))
        with self.coordinator.guard:
            self.coordinator.backend_pids.add(backend_pid)
        self.coordinator.attempt_barrier.wait(timeout=10)
        lease = self.delegate.acquire(
            db,
            demo_session_id=demo_session_id,
        )
        if lease is None:
            self.coordinator.loser_attempted.set()
            return None
        if not self.coordinator.loser_attempted.wait(timeout=10):
            self.delegate.release(
                db,
                demo_session_id=demo_session_id,
                lease=lease,
            )
            raise RuntimeError("The competing advisory attempt did not run.")
        return lease

    def release(self, db, *, demo_session_id, lease):
        self.delegate.release(
            db,
            demo_session_id=demo_session_id,
            lease=lease,
        )


class _FailTargetHandoffDelete(DemoHandoffEventRepository):
    def __init__(self, target_session_id):
        self.target_session_id = target_session_id

    def delete_by_demo_session(self, db, *, demo_session_id):
        deleted = super().delete_by_demo_session(
            db,
            demo_session_id=demo_session_id,
        )
        if demo_session_id == self.target_session_id:
            raise RuntimeError("forced handoff rollback")
        return deleted


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class DemoRateLimitCleanupPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(
            os.environ["TEST_DATABASE_URL"],
            pool_pre_ping=True,
        )
        with cls.admin.connect() as connection:
            identity = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
        if identity != ("aura_test", "aura_test_runner"):
            cls.admin.dispose()
            raise RuntimeError(
                "Dedicated PostgreSQL preflight identity did not match."
            )
        cls.schema = f"aura_demo_operational_test_{uuid4().hex[:12]}"
        cls.resources = DisposableSchemaResources(
            admin_engine=cls.admin,
            schema=cls.schema,
            allowed_prefixes=("aura_demo_operational_test_",),
            dispose_admin=True,
        )
        cls.addClassCleanup(cls.resources.cleanup)
        with cls.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        schema_url = make_url(
            os.environ["TEST_DATABASE_URL"]
        ).update_query_dict(
            {"options": f"-csearch_path={cls.schema},public"}
        )
        cls.engine = create_engine(
            schema_url,
            pool_pre_ping=True,
            pool_size=20,
            max_overflow=20,
        )
        cls.resources.track_engine(cls.engine)
        Customer.__table__.create(cls.engine)
        Reservation.__table__.create(cls.engine)
        ConversationWorkflowState.__table__.create(cls.engine)
        SupportTicket.__table__.create(cls.engine)
        SupportTicketNotification.__table__.create(cls.engine)
        TelegramIdentity.__table__.create(cls.engine)
        migrate_demo(cls.engine, schema=cls.schema)
        if not migrate_request_id(cls.engine, schema=cls.schema):
            raise RuntimeError("Request ID migration did not apply.")
        cls.Session = sessionmaker(
            bind=cls.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE TABLE "
                    "support_ticket_notifications, support_tickets, "
                    "telegram_identities, conversation_workflow_states, "
                    "reservations, demo_rate_limit_buckets, "
                    "demo_handoff_events, demo_chat_messages, demo_sessions, "
                    "customers RESTART IDENTITY CASCADE"
                )
            )
        self.now = datetime(2026, 8, 4, 12, 0, 1, tzinfo=timezone.utc)
        self.create_session(TOKEN_A)
        self.create_session(TOKEN_B)

    def create_session(self, token):
        with self.Session() as db:
            DemoSessionService(
                token_generator=lambda: token,
                clock=lambda: self.now,
            ).create_session(db)

    def digest(self, token):
        return digest_demo_session_token(token)

    def test_atomic_concurrency_has_no_lost_increment_or_limit_bypass(self):
        def consume():
            with self.Session() as db:
                service = DemoRateLimitService(clock=lambda: self.now)
                window_start, expires_at = service._window(
                    self.now,
                    3600,
                )
                with UnitOfWork(db) as unit:
                    count = DemoRateLimitBucketRepository().consume_atomic(
                        db,
                        scope_type="session",
                        subject_digest=self.digest(TOKEN_A),
                        action="reset",
                        window_started_at=window_start,
                        window_seconds=3600,
                        expires_at=expires_at,
                        now=self.now,
                    )
                    unit.commit()
                return count <= 5

        def concurrent_wave(size):
            barrier = threading.Barrier(size)

            def worker():
                barrier.wait(timeout=10)
                return consume()

            with ThreadPoolExecutor(max_workers=size) as executor:
                return list(
                    executor.map(
                        lambda _index: worker(),
                        range(size),
                    )
                )

        first_wave = concurrent_wave(3)
        middle = consume()
        crossing_wave = concurrent_wave(2)
        allowed = first_wave + [middle] + crossing_wave
        self.assertEqual(sum(allowed), 5)
        with self.Session() as db:
            rows = list(
                db.scalars(
                    select(DemoRateLimitBucket).where(
                        DemoRateLimitBucket.scope_type == "session",
                        DemoRateLimitBucket.action == "reset",
                        DemoRateLimitBucket.subject_digest
                        == self.digest(TOKEN_A),
                    )
                )
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].request_count, 6)

    def test_postgresql_upsert_enforces_and_preserves_expiry_invariant(self):
        service = DemoRateLimitService(clock=lambda: self.now)
        window_start, canonical_expiry = service._window(
            self.now,
            60,
        )
        repository = DemoRateLimitBucketRepository()
        shorter_expiry = canonical_expiry - timedelta(seconds=20)
        longer_expiry = canonical_expiry + timedelta(seconds=60)
        identities = (
            ("chat", self.digest(TOKEN_A), shorter_expiry),
            ("reset", self.digest(TOKEN_A), longer_expiry),
            ("session_current", self.digest(TOKEN_B), canonical_expiry),
        )
        with self.Session() as db:
            with self.assertRaisesRegex(ValueError, "canonical window"):
                repository.consume_atomic(
                    db,
                    scope_type="session",
                    subject_digest=self.digest(TOKEN_A),
                    action="chat",
                    window_started_at=window_start,
                    window_seconds=60,
                    expires_at=canonical_expiry - timedelta(seconds=1),
                    now=self.now,
                )
            db.add_all(
                [
                    DemoRateLimitBucket(
                        scope_type="session",
                        subject_digest=subject_digest,
                        action=action,
                        window_started_at=window_start,
                        window_seconds=60,
                        request_count=1,
                        expires_at=existing_expiry,
                        updated_at=self.now,
                    )
                    for action, subject_digest, existing_expiry in identities
                ]
            )
            db.commit()
            for action, subject_digest, _existing_expiry in identities:
                with UnitOfWork(db) as unit:
                    count = repository.consume_atomic(
                        db,
                        scope_type="session",
                        subject_digest=subject_digest,
                        action=action,
                        window_started_at=window_start,
                        window_seconds=60,
                        expires_at=canonical_expiry,
                        now=self.now,
                    )
                    unit.commit()
                self.assertEqual(count, 2)
            rows = {
                row.action: row
                for row in db.scalars(
                    select(DemoRateLimitBucket).where(
                        DemoRateLimitBucket.scope_type == "session",
                        DemoRateLimitBucket.action.in_(
                            [item[0] for item in identities]
                        ),
                    )
                )
            }
        self.assertEqual(rows["chat"].expires_at, shorter_expiry)
        self.assertEqual(rows["reset"].expires_at, longer_expiry)
        self.assertEqual(
            rows["session_current"].expires_at,
            canonical_expiry,
        )

    def test_session_action_isolation_and_global_chat_cap(self):
        with self.Session() as db:
            rate_limits = DemoRateLimitService(clock=lambda: self.now)
            rate_limits.enforce(
                db,
                action=DemoRateLimitAction.RESET,
                session_token_digest=self.digest(TOKEN_A),
            )
            rate_limits.enforce(
                db,
                action=DemoRateLimitAction.SESSION_CURRENT,
                session_token_digest=self.digest(TOKEN_A),
            )
            rate_limits.enforce(
                db,
                action=DemoRateLimitAction.RESET,
                session_token_digest=self.digest(TOKEN_B),
            )
            for index in range(301):
                try:
                    rate_limits.enforce(
                        db,
                        action=DemoRateLimitAction.CHAT,
                        session_token_digest=(
                            self.digest(TOKEN_A)
                            if index % 2
                            else self.digest(TOKEN_B)
                        ),
                    )
                except DemoRateLimitExceededError:
                    pass
            rows = list(db.scalars(select(DemoRateLimitBucket)))
        global_chat = next(
            row
            for row in rows
            if row.scope_type == "global" and row.action == "chat"
        )
        self.assertEqual(global_chat.request_count, 301)
        reset_rows = [
            row
            for row in rows
            if row.scope_type == "session" and row.action == "reset"
        ]
        self.assertEqual(len(reset_rows), 2)
        self.assertEqual({row.request_count for row in reset_rows}, {1})

    def expire(self, token):
        with self.Session() as db:
            row = db.scalar(
                select(DemoSession).where(
                    DemoSession.token_digest == self.digest(token)
                )
            )
            row.idle_expires_at = self.now
            row.updated_at = self.now
            db.commit()
            return row.id

    def test_cleanup_deletes_expired_owner_data_and_preserves_active_session(self):
        expired_id = self.expire(TOKEN_A)
        with self.Session() as db:
            db.add(
                DemoChatMessage(
                    demo_session_id=expired_id,
                    role="user",
                    content="marker",
                    created_at=self.now,
                )
            )
            db.commit()
        summary = asyncio.run(
            DemoCleanupService(
                session_factory=self.Session,
                app_env="demo",
                clock=lambda: self.now,
            ).run_once()
        )
        self.assertEqual(summary.cleaned_sessions, 1)
        with self.Session() as db:
            self.assertIsNone(
                db.scalar(
                    select(DemoSession).where(
                        DemoSession.id == expired_id
                    )
                )
            )
            self.assertIsNotNone(
                db.scalar(
                    select(DemoSession).where(
                        DemoSession.token_digest == self.digest(TOKEN_B)
                    )
                )
            )
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(DemoChatMessage)
                ),
                0,
            )

    def test_handoff_delete_failure_rolls_back_and_next_session_continues(self):
        failed_session_id = self.expire(TOKEN_A)
        next_session_id = self.expire(TOKEN_B)
        with self.Session() as db:
            failed_session = db.get(DemoSession, failed_session_id)
            next_session = db.get(DemoSession, next_session_id)
            failed_owner_id = failed_session.owner_customer_id
            next_owner_id = next_session.owner_customer_id
            db.add_all(
                [
                    DemoChatMessage(
                        demo_session_id=failed_session_id,
                        role="user",
                        content="rollback marker",
                        created_at=self.now,
                    ),
                    ConversationWorkflowState(
                        owner_customer_id=failed_owner_id,
                        session_reference_hash=(
                            ConversationWorkflowStateService
                            .hash_session_reference(
                                f"demo-session-{failed_session_id}"
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
                        demo_session_id=failed_session_id,
                        reference="DEMO-HO-FA17FA17",
                        status="simulated",
                        reason_code="internal_error",
                        safe_summary=(
                            "The demo assistant could not safely complete "
                            "the request."
                        ),
                        created_at=self.now,
                    ),
                    Reservation(
                        name="Rollback",
                        people=2,
                        date="2026-08-05",
                        time="19:00",
                        owner_customer_id=failed_owner_id,
                    ),
                    DemoRateLimitBucket(
                        scope_type="session",
                        subject_digest=demo_session_rate_limit_subject(
                            failed_session.token_digest
                        ),
                        action="chat",
                        window_started_at=self.now,
                        window_seconds=60,
                        request_count=3,
                        expires_at=self.now + timedelta(minutes=1),
                        updated_at=self.now,
                    ),
                ]
            )
            db.commit()

        summary = asyncio.run(
            DemoCleanupService(
                session_factory=self.Session,
                app_env="demo",
                handoff_repository=_FailTargetHandoffDelete(
                    failed_session_id
                ),
                clock=lambda: self.now,
            ).run_once(batch_size=2)
        )

        with self.Session() as verification_db:
            with UnitOfWork(verification_db) as unit:
                self.assertIsNotNone(
                    verification_db.get(DemoSession, failed_session_id)
                )
                self.assertIsNone(
                    verification_db.get(DemoSession, next_session_id)
                )
                self.assertIsNotNone(
                    verification_db.get(Customer, failed_owner_id)
                )
                self.assertIsNone(
                    verification_db.get(Customer, next_owner_id)
                )
                for model in (
                    DemoChatMessage,
                    ConversationWorkflowState,
                    DemoHandoffEvent,
                    Reservation,
                    DemoRateLimitBucket,
                ):
                    self.assertEqual(
                        verification_db.scalar(
                            select(func.count()).select_from(model)
                        ),
                        1,
                    )
                unit.commit()
        self.assertEqual(summary.scanned, 2)
        self.assertEqual(summary.failed_sessions, 1)
        self.assertEqual(summary.cleaned_sessions, 1)
        summary_values = asdict(summary)
        self.assertEqual(
            set(summary_values),
            {
                "scanned",
                "cleaned_sessions",
                "skipped_locked",
                "skipped_not_eligible",
                "failed_sessions",
                "deleted_expired_buckets",
            },
        )
        rendered = repr(summary_values)
        self.assertNotIn("forced handoff rollback", rendered)

    def test_chat_lock_conflict_skips_and_independent_session_cleans(self):
        session_a_id = self.expire(TOKEN_A)
        self.expire(TOKEN_B)
        lock = DemoPostgreSQLAdvisoryLock()
        with self.Session() as lock_db:
            lease = lock.acquire(
                lock_db,
                demo_session_id=session_a_id,
            )
            self.assertIsNotNone(lease)
            try:
                summary = asyncio.run(
                    DemoCleanupService(
                        session_factory=self.Session,
                        app_env="demo",
                        clock=lambda: self.now,
                    ).run_once()
                )
            finally:
                lock.release(
                    lock_db,
                    demo_session_id=session_a_id,
                    lease=lease,
                )
        self.assertEqual(summary.skipped_locked, 1)
        self.assertEqual(summary.cleaned_sessions, 1)
        with self.Session() as db:
            remaining = list(db.scalars(select(DemoSession.id)))
        self.assertEqual(remaining, [session_a_id])

    def test_two_cleanup_workers_do_not_double_delete(self):
        expired_id = self.expire(TOKEN_A)
        with self.Session() as db:
            db.add(
                DemoChatMessage(
                    demo_session_id=expired_id,
                    role="user",
                    content="must be deleted exactly once",
                    created_at=self.now,
                )
            )
            db.commit()

        scan_barrier = threading.Barrier(2)
        coordinator = _AdvisoryRaceCoordinator()
        lock_managers = [
            ConversationLockManager(),
            ConversationLockManager(),
        ]
        self.assertIsNot(lock_managers[0], lock_managers[1])
        services = [
            DemoCleanupService(
                session_factory=self.Session,
                app_env="demo",
                session_repository=_BarrierScanSessionRepository(
                    scan_barrier
                ),
                lock_manager=lock_managers[index],
                database_lock=_CoordinatedAdvisoryLock(coordinator),
                clock=lambda: self.now,
            )
            for index in range(2)
        ]

        def run(service):
            return asyncio.run(service.run_once(batch_size=1))

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, services))

        self.assertEqual([item.scanned for item in results], [1, 1])
        self.assertEqual(
            sum(item.cleaned_sessions for item in results),
            1,
        )
        self.assertEqual(sum(item.failed_sessions for item in results), 0)
        self.assertEqual(
            sum(
                item.skipped_locked + item.skipped_not_eligible
                for item in results
            ),
            1,
        )
        self.assertEqual(len(coordinator.backend_pids), 2)
        with self.Session() as db:
            self.assertIsNone(db.get(DemoSession, expired_id))
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(DemoChatMessage)
                ),
                0,
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Customer)),
                1,
            )
        rendered = " ".join(str(asdict(item)) for item in results)
        self.assertNotIn(self.digest(TOKEN_A), rendered)
        self.assertEqual(
            set(asdict(results[0])),
            {
                "scanned",
                "cleaned_sessions",
                "skipped_locked",
                "skipped_not_eligible",
                "failed_sessions",
                "deleted_expired_buckets",
            },
        )

    def test_expired_bucket_cleanup_is_capped_and_resumable(self):
        batch_size = 2
        expired_rows = []
        with self.Session() as db:
            for index in range(batch_size + 3):
                row = DemoRateLimitBucket(
                    scope_type=("session", "global", "ip")[index % 3],
                    subject_digest=f"{index + 1:064x}",
                    action="chat",
                    window_started_at=self.now - timedelta(minutes=10),
                    window_seconds=60,
                    request_count=index + 1,
                    expires_at=self.now - timedelta(
                        seconds=batch_size + 3 - index
                    ),
                    updated_at=self.now,
                )
                db.add(row)
                expired_rows.append(row)
            for index, scope in enumerate(("session", "global", "ip")):
                db.add(
                    DemoRateLimitBucket(
                        scope_type=scope,
                        subject_digest=f"{index + 100:064x}",
                        action="chat",
                        window_started_at=self.now,
                        window_seconds=60,
                        request_count=7,
                        expires_at=self.now + timedelta(minutes=1),
                        updated_at=self.now,
                    )
                )
            db.commit()
            expired_ids = [row.id for row in expired_rows]

        service = DemoCleanupService(
            session_factory=self.Session,
            app_env="demo",
            clock=lambda: self.now,
        )
        summaries = []
        expired_backlog = []
        remaining_expired_ids = []
        for _ in range(4):
            summaries.append(
                asyncio.run(service.run_once(batch_size=batch_size))
            )
            with self.Session() as db:
                remaining = set(
                    db.scalars(
                        select(DemoRateLimitBucket.id).where(
                            DemoRateLimitBucket.expires_at <= self.now
                        )
                    )
                )
            remaining_expired_ids.append(remaining)
            expired_backlog.append(len(remaining))
        self.assertEqual(
            [item.deleted_expired_buckets for item in summaries],
            [2, 2, 1, 0],
        )
        self.assertEqual(expired_backlog, [3, 1, 0, 0])
        self.assertTrue(
            set(expired_ids[:2]).isdisjoint(remaining_expired_ids[0])
        )
        self.assertTrue(
            set(expired_ids[2:]).issubset(remaining_expired_ids[0])
        )
        self.assertTrue(
            all(
                item.deleted_expired_buckets <= batch_size
                for item in summaries
            )
        )
        with self.Session() as db:
            remaining_after_all = set(
                db.scalars(select(DemoRateLimitBucket.id))
            )
            active_count = db.scalar(
                select(func.count())
                .select_from(DemoRateLimitBucket)
                .where(DemoRateLimitBucket.expires_at > self.now)
            )
        self.assertTrue(set(expired_ids).isdisjoint(remaining_after_all))
        self.assertEqual(active_count, 3)

    def test_reset_held_advisory_lock_makes_cleanup_skip(self):
        session_id = self.expire(TOKEN_A)
        reset_lock = DemoPostgreSQLAdvisoryLock()
        with self.Session() as reset_db:
            lease = reset_lock.acquire(
                reset_db,
                demo_session_id=session_id,
            )
            self.assertIsNotNone(lease)
            try:
                summary = asyncio.run(
                    DemoCleanupService(
                        session_factory=self.Session,
                        app_env="demo",
                        clock=lambda: self.now,
                    ).run_once(batch_size=1)
                )
            finally:
                reset_lock.release(
                    reset_db,
                    demo_session_id=session_id,
                    lease=lease,
                )
        self.assertEqual(summary.skipped_locked, 1)
        self.assertEqual(summary.cleaned_sessions, 0)
        with self.Session() as db:
            self.assertIsNotNone(db.get(DemoSession, session_id))
