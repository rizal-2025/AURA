"""Offline service and repository tests for demo reservation read/reset."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.core.conversation_lock_manager import ConversationLockManager
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
from app.db.repositories.reservation_repository import ReservationRepository
from app.db.repositories.demo_persistence_repository import (
    demo_session_rate_limit_subject,
)
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from app.services.demo_chat_errors import (
    DemoChatRequestConflictError,
    DemoChatServiceUnavailableError,
)
from app.services.demo_chat_service import demo_chat_service
from app.services.demo_reservation_reset_service import (
    DemoReservationResetService,
)
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    digest_demo_session_token,
)


TOKEN_A = "R" * 43
TOKEN_B = "S" * 43


class _NoopDatabaseLock:
    def __init__(self, *, available=True):
        self.available = available
        self.acquired = []
        self.released = []

    def acquire(self, _db, *, demo_session_id):
        self.acquired.append(demo_session_id)
        return object() if self.available else None

    def release(self, _db, *, demo_session_id, lease):
        self.released.append((demo_session_id, lease))


class _FailingReservationRepository(ReservationRepository):
    def delete_by_owner_customer_id(self, db, owner_customer_id):
        super().delete_by_owner_customer_id(db, owner_customer_id)
        raise RuntimeError("forced reset failure")


class DemoReservationResetServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.create_function("char_length", 1, len)
            connection.create_function(
                "jsonb_typeof",
                1,
                lambda value: (
                    "object"
                    if isinstance(value, str)
                    and value.lstrip().startswith("{")
                    else "other"
                ),
            )

        Customer.__table__.create(self.engine)
        Reservation.__table__.create(self.engine)
        ConversationWorkflowState.__table__.create(self.engine)
        DemoSession.__table__.create(self.engine)
        DemoChatMessage.__table__.create(self.engine)
        DemoHandoffEvent.__table__.create(self.engine)
        DemoRateLimitBucket.__table__.create(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        self.session_service = DemoSessionService(
            token_generator=lambda: TOKEN_A,
            clock=lambda: self.now,
        )
        self.session_service.create_session(self.db)
        DemoSessionService(
            token_generator=lambda: TOKEN_B,
            clock=lambda: self.now,
        ).create_session(self.db)
        self.session_a = self._session(TOKEN_A)
        self.session_b = self._session(TOKEN_B)
        self.owner_a = self.db.get(Customer, self.session_a.owner_customer_id)
        self.owner_b = self.db.get(Customer, self.session_b.owner_customer_id)
        self.reservation_counter = 0

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _session(self, token):
        return self.db.scalar(
            select(DemoSession).where(
                DemoSession.token_digest == digest_demo_session_token(token)
            )
        )

    def service(self, **kwargs):
        return DemoReservationResetService(
            session_service=kwargs.pop(
                "session_service",
                DemoSessionService(clock=lambda: self.now),
            ),
            lock_manager=kwargs.pop("lock_manager", ConversationLockManager()),
            database_lock=kwargs.pop("database_lock", _NoopDatabaseLock()),
            clock=lambda: self.now,
            **kwargs,
        )

    def add_reservation(
        self,
        owner,
        *,
        date="2026-08-02",
        time="19:00",
        status="pending",
    ):
        self.reservation_counter += 1
        row = Reservation(
            name="Demo",
            people=4,
            date=date,
            time=time,
            owner_customer_id=owner.id,
            status=status,
            public_reference=f"RSV_{self.reservation_counter:032x}",
        )
        self.db.add(row)
        self.db.flush()
        return row

    def seed_reset_data(self):
        request_id = uuid4()
        self.db.add_all(
            [
                DemoChatMessage(
                    demo_session_id=self.session_a.id,
                    role="user",
                    content="completed",
                    request_id=request_id,
                    created_at=self.now,
                ),
                DemoChatMessage(
                    demo_session_id=self.session_a.id,
                    role="assistant",
                    content="reply",
                    request_id=request_id,
                    created_at=self.now,
                ),
                DemoChatMessage(
                    demo_session_id=self.session_a.id,
                    role="user",
                    content="incomplete",
                    request_id=uuid4(),
                    created_at=self.now,
                ),
                DemoChatMessage(
                    demo_session_id=self.session_b.id,
                    role="user",
                    content="other",
                    created_at=self.now,
                ),
                DemoHandoffEvent(
                    demo_session_id=self.session_a.id,
                    reference="DEMO-HO-A",
                    status="simulated",
                    reason_code="explicit_human_request",
                    safe_summary=(
                        "Demo visitor requested simulated human assistance."
                    ),
                    created_at=self.now,
                ),
            ]
        )
        self.db.add(
            ConversationWorkflowState(
                owner_customer_id=self.owner_a.id,
                session_reference_hash=(
                    ConversationWorkflowStateService.hash_session_reference(
                        f"demo-session-{self.session_a.id}"
                    )
                ),
                schema_version=1,
                payload={"intent": "reservation"},
                is_active=True,
                revision=1,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        self.add_reservation(self.owner_a)
        self.add_reservation(self.owner_b)
        for scope, digest in (
            (
                "session",
                demo_session_rate_limit_subject(
                    self.session_a.token_digest
                ),
            ),
            (
                "session",
                demo_session_rate_limit_subject(
                    self.session_b.token_digest
                ),
            ),
            ("ip", "a" * 64),
            ("global", "b" * 64),
        ):
            self.db.add(
                DemoRateLimitBucket(
                    scope_type=scope,
                    subject_digest=digest,
                    action="chat",
                    window_started_at=self.now,
                    window_seconds=60,
                    request_count=1,
                    expires_at=self.now + timedelta(minutes=1),
                    updated_at=self.now,
                )
            )
        self.db.commit()

    def test_default_service_reuses_chat_lock_primitives(self):
        service = DemoReservationResetService()
        self.assertIs(service.lock_manager, demo_chat_service.lock_manager)
        self.assertIs(service.database_lock, demo_chat_service.database_lock)
        self.assertEqual(
            service._process_lock_key(17),
            "demo-chat-session-17",
        )

    def test_session_rate_limit_subject_is_canonical_and_fail_closed(self):
        digest = "a" * 64
        self.assertEqual(
            demo_session_rate_limit_subject(digest),
            digest,
        )
        for malformed in (
            None,
            17,
            "",
            "a" * 63,
            "A" * 64,
            "g" * 64,
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(
                    ValueError,
                    "^A valid demo digest is required\\.$",
                ):
                    demo_session_rate_limit_subject(malformed)

    def test_repository_owner_methods_reject_missing_owner(self):
        repository = ReservationRepository()
        for operation in (
            lambda: repository.list_for_owner(self.db, None),
            lambda: repository.count_for_owner(self.db, None),
            lambda: repository.delete_by_owner_customer_id(self.db, None),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()

    def test_list_is_owner_scoped_limited_counted_and_deterministic(self):
        for index in range(55):
            self.add_reservation(
                self.owner_a,
                date=f"2026-08-{(index % 9) + 1:02d}",
                time=f"{(index % 5) + 10:02d}:00",
            )
        self.add_reservation(self.owner_b, date="2026-01-01")
        self.db.commit()
        expected_rows = ReservationRepository().list_for_owner(
            self.db,
            self.owner_a.id,
            limit=50,
        )

        result = self.service().list_reservations(
            self.db,
            raw_session_token=TOKEN_A,
        )

        self.assertEqual(result.count, 55)
        self.assertEqual(len(result.reservations), 50)
        self.assertNotIn(
            "2026-01-01",
            {
                reservation.reservation_date.isoformat()
                for reservation in result.reservations
            },
        )
        self.assertEqual(
            [
                (
                    reservation.status,
                    reservation.reservation_date.isoformat(),
                    reservation.reservation_time.strftime("%H:%M"),
                    reservation.party_size,
                )
                for reservation in result.reservations
            ],
            [
                (
                    row.status,
                    row.date,
                    row.time,
                    row.people,
                )
                for row in expected_rows
            ],
        )
        internal_ordering = [
            (
                row.date,
                row.time,
                row.id,
            )
            for row in expected_rows
        ]
        self.assertEqual(internal_ordering, sorted(internal_ordering))

    def test_list_rejects_invalid_expired_and_revoked_sessions(self):
        service = self.service()
        with self.assertRaises(DemoSessionRequiredError):
            service.list_reservations(
                self.db,
                raw_session_token="T" * 43,
            )
        self.session_a.revoked_at = self.now
        self.db.commit()
        with self.assertRaises(DemoSessionRequiredError):
            service.list_reservations(
                self.db,
                raw_session_token=TOKEN_A,
            )

    def test_unsupported_persisted_status_fails_closed(self):
        self.add_reservation(self.owner_a, status="internal-only")
        self.db.commit()
        with self.assertRaises(DemoChatServiceUnavailableError):
            self.service().list_reservations(
                self.db,
                raw_session_token=TOKEN_A,
            )

    def test_reset_is_scoped_idempotent_and_retains_identity(self):
        self.seed_reset_data()
        original_absolute_expiry = self.session_a.absolute_expires_at
        lock = _NoopDatabaseLock()
        service = self.service(database_lock=lock)

        first = asyncio.run(
            service.reset(self.db, raw_session_token=TOKEN_A)
        )
        second = asyncio.run(
            service.reset(self.db, raw_session_token=TOKEN_A)
        )

        self.assertEqual(first.status, "reset")
        self.assertEqual(second.status, "reset")
        self.assertEqual(first.session.message_count, 0)
        self.assertEqual(first.reservation_count, 0)
        self.assertIsNone(first.handoff)
        retained_session = self._session(TOKEN_A)
        self.assertIsNotNone(retained_session)
        self.assertIsNotNone(self.db.get(Customer, self.owner_a.id))
        if original_absolute_expiry.tzinfo is None:
            original_absolute_expiry = original_absolute_expiry.replace(
                tzinfo=timezone.utc
            )
        self.assertEqual(
            retained_session.absolute_expires_at,
            original_absolute_expiry,
        )
        self.assertEqual(
            self.db.scalar(
                select(func.count())
                .select_from(DemoChatMessage)
                .where(DemoChatMessage.demo_session_id == self.session_a.id)
            ),
            0,
        )
        self.assertEqual(
            self.db.scalar(
                select(func.count())
                .select_from(ConversationWorkflowState)
                .where(
                    ConversationWorkflowState.owner_customer_id
                    == self.owner_a.id
                )
            ),
            0,
        )
        self.assertEqual(
            self.db.scalar(
                select(func.count())
                .select_from(DemoHandoffEvent)
                .where(DemoHandoffEvent.demo_session_id == self.session_a.id)
            ),
            0,
        )
        self.assertEqual(
            self.db.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.owner_customer_id == self.owner_a.id)
            ),
            0,
        )
        self.assertEqual(
            self.db.scalar(
                select(func.count())
                .select_from(DemoChatMessage)
                .where(DemoChatMessage.demo_session_id == self.session_b.id)
            ),
            1,
        )
        self.assertEqual(
            self.db.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.owner_customer_id == self.owner_b.id)
            ),
            1,
        )
        remaining_scopes = set(
            self.db.scalars(
                select(DemoRateLimitBucket.scope_type)
            )
        )
        self.assertEqual(remaining_scopes, {"session", "ip", "global"})
        self.assertEqual(len(lock.acquired), 2)
        self.assertEqual(len(lock.released), 2)

    def test_reset_failure_rolls_back_every_delete(self):
        self.seed_reset_data()
        service = self.service(
            reservation_repository=_FailingReservationRepository(),
        )
        with self.assertRaises(DemoChatServiceUnavailableError):
            asyncio.run(service.reset(self.db, raw_session_token=TOKEN_A))
        self.assertGreater(
            self.db.scalar(
                select(func.count())
                .select_from(DemoChatMessage)
                .where(DemoChatMessage.demo_session_id == self.session_a.id)
            ),
            0,
        )
        self.assertEqual(
            self.db.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.owner_customer_id == self.owner_a.id)
            ),
            1,
        )
        self.assertEqual(
            self.db.scalar(
                select(func.count())
                .select_from(DemoHandoffEvent)
                .where(DemoHandoffEvent.demo_session_id == self.session_a.id)
            ),
            1,
        )

    def test_unavailable_advisory_lock_is_request_conflict(self):
        service = self.service(
            database_lock=_NoopDatabaseLock(available=False),
        )
        with self.assertRaises(DemoChatRequestConflictError):
            asyncio.run(service.reset(self.db, raw_session_token=TOKEN_A))

    def test_revoked_session_cannot_reset(self):
        self.session_a.revoked_at = self.now
        self.db.commit()
        with self.assertRaises(DemoSessionRequiredError):
            asyncio.run(
                self.service().reset(
                    self.db,
                    raw_session_token=TOKEN_A,
                )
            )


if __name__ == "__main__":
    unittest.main()
