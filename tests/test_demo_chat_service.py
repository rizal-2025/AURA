"""Offline service tests for persistent, owner-scoped demo chat."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.brain.memory_manager import MemoryManager
from app.core.transaction_errors import PersistenceOperationError
from app.db.models.customer import Customer
from app.db.models.demo_persistence import (
    SAFE_DEMO_HANDOFF_SUMMARIES,
    DemoChatMessage,
    DemoHandoffEvent,
    DemoSession,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
)
from app.services.demo_chat_service import (
    DemoChatProviderError,
    DemoChatProviderTimeoutError,
    DemoChatRequestConflictError,
    DemoPostgreSQLAdvisoryLock,
    DemoChatService,
    DemoChatServiceUnavailableError,
    DemoSimulatedHandoffService,
)
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    digest_demo_session_token,
)


TOKEN_A = "A" * 43
TOKEN_B = "B" * 43


class _NoopDatabaseLock:
    def __init__(self):
        self.acquired = []
        self.released = []

    def acquire(self, _db, *, demo_session_id):
        self.acquired.append(demo_session_id)
        return True

    def release(self, _db, *, demo_session_id, lease):
        self.asserted_lease = lease
        self.released.append(demo_session_id)


class _FakeCore:
    def __init__(self, calls, *, reply="Jawaban aman.", error=None):
        self.calls = calls
        self.reply = reply
        self.error = error

    async def process(
        self,
        *,
        db,
        customer,
        session_reference,
        message,
    ):
        self.calls.append(
            {
                "db": db,
                "customer": customer,
                "session_reference": session_reference,
                "message": message,
            }
        )
        if self.error is not None:
            raise self.error
        return self.reply


class _AssistantFailingRepository(DemoChatMessageRepository):
    def append_request_message(self, db, **values):
        if values["role"] == "assistant":
            raise RuntimeError("unsafe database detail")
        return super().append_request_message(db, **values)


class _FakeConnection:
    def __init__(self, *scalar_results):
        self.scalar_results = list(scalar_results)
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.invalidations = 0

    def scalar(self, *_args, **_kwargs):
        result = self.scalar_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1

    def invalidate(self):
        self.invalidations += 1


class _FakePostgreSQLBind:
    class _Dialect:
        name = "postgresql"

    dialect = _Dialect()

    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


class _FakeDatabaseSession:
    def __init__(self, connection):
        self.bind = _FakePostgreSQLBind(connection)

    def get_bind(self):
        return self.bind


class _FailingResolveSessionService:
    def resolve_active_session(self, *_args, **_kwargs):
        raise PersistenceOperationError()


class DemoChatServiceTests(unittest.TestCase):
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
        self.now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        self.calls = []
        self.lock = _NoopDatabaseLock()
        self.session_service = DemoSessionService(
            token_generator=lambda: TOKEN_A,
            clock=lambda: self.now,
        )
        self.session_service.create_session(self.db)
        self.session_a = self.db.scalar(
            select(DemoSession).where(
                DemoSession.token_digest
                == digest_demo_session_token(TOKEN_A)
            )
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(
        self,
        *,
        reply="Jawaban aman.",
        error=None,
        repository=None,
        session_service=None,
        calls=None,
    ):
        active_calls = self.calls if calls is None else calls
        return DemoChatService(
            session_service=session_service or self.session_service,
            message_repository=repository,
            database_lock=self.lock,
            core_factory=lambda _session_id: _FakeCore(
                active_calls,
                reply=reply,
                error=error,
            ),
            clock=lambda: self.now,
        )

    def process(
        self,
        service,
        *,
        token=TOKEN_A,
        message="Halo AURA",
        request_id=None,
    ):
        return asyncio.run(
            service.process(
                self.db,
                raw_session_token=token,
                message=message,
                request_id=request_id or uuid4(),
            )
        )

    def create_second_session(self):
        service = DemoSessionService(
            token_generator=lambda: TOKEN_B,
            clock=lambda: self.now,
        )
        service.create_session(self.db)
        return self.db.scalar(
            select(DemoSession).where(
                DemoSession.token_digest
                == digest_demo_session_token(TOKEN_B)
            )
        )

    def request_rows(self, session_id, request_id):
        return DemoChatMessageRepository().list_by_request_id(
            self.db,
            demo_session_id=session_id,
            request_id=request_id,
        )

    def test_success_persists_user_then_assistant_and_returns_stored_row(self):
        request_id = uuid4()
        response = self.process(
            self.service(),
            message="Pesan pengguna",
            request_id=request_id,
        )
        rows = self.request_rows(self.session_a.id, request_id)
        self.assertEqual(
            [(row.role, row.content) for row in rows],
            [
                ("user", "Pesan pengguna"),
                ("assistant", "Jawaban aman."),
            ],
        )
        self.assertEqual(response.reply.id, rows[1].id)
        self.assertEqual(response.reply.content, rows[1].content)
        self.assertEqual(response.reply.created_at, rows[1].created_at)
        self.assertIsNotNone(rows[0].created_at.tzinfo)
        self.assertIsNotNone(rows[1].created_at.tzinfo)

    def test_core_receives_owner_identity_and_stable_server_reference(self):
        self.process(self.service())
        call = self.calls[0]
        self.assertEqual(call["customer"].id, self.session_a.owner_customer_id)
        self.assertEqual(
            call["session_reference"],
            f"demo-session-{self.session_a.id}",
        )
        rendered = repr(call)
        self.assertNotIn(TOKEN_A, rendered)
        self.assertNotIn(self.session_a.token_digest, rendered)

    def test_completed_replay_uses_database_and_does_not_call_core_twice(self):
        request_id = uuid4()
        first = self.process(self.service(), request_id=request_id)
        restarted_calls = []
        second = self.process(
            self.service(calls=restarted_calls),
            request_id=request_id,
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(restarted_calls, [])
        self.assertEqual(
            len(self.request_rows(self.session_a.id, request_id)),
            2,
        )

    def test_completed_replay_with_different_message_is_conflict(self):
        request_id = uuid4()
        self.process(
            self.service(),
            message="Pesan pertama",
            request_id=request_id,
        )
        replay_calls = []
        with self.assertRaises(DemoChatRequestConflictError):
            self.process(
                self.service(calls=replay_calls),
                message="Pesan berbeda",
                request_id=request_id,
            )
        self.assertEqual(replay_calls, [])

    def test_replay_uses_same_newline_canonicalization_as_persistence(self):
        request_id = uuid4()
        first = self.process(
            self.service(),
            message="baris satu\r\nbaris dua",
            request_id=request_id,
        )
        replay_calls = []
        replay = self.process(
            self.service(calls=replay_calls),
            message="baris satu\rbaris dua",
            request_id=request_id,
        )
        self.assertEqual(first, replay)
        self.assertEqual(replay_calls, [])
        self.assertEqual(
            self.request_rows(self.session_a.id, request_id)[0].content,
            "baris satu\nbaris dua",
        )

    def test_incomplete_request_is_conflict_and_never_reinvokes_core(self):
        request_id = uuid4()
        DemoChatMessageRepository().append_request_message(
            self.db,
            demo_session_id=self.session_a.id,
            role="user",
            content="Halo AURA",
            request_id=request_id,
            created_at=self.now,
        )
        self.db.commit()
        with self.assertRaises(DemoChatRequestConflictError):
            self.process(self.service(), request_id=request_id)
        self.assertEqual(self.calls, [])

    def test_incomplete_different_message_is_conflict_without_disclosure(self):
        request_id = uuid4()
        previous_message = "pesan lama yang sensitif"
        DemoChatMessageRepository().append_request_message(
            self.db,
            demo_session_id=self.session_a.id,
            role="user",
            content=previous_message,
            request_id=request_id,
            created_at=self.now,
        )
        self.db.commit()
        with self.assertRaises(DemoChatRequestConflictError) as captured:
            self.process(
                self.service(),
                message="pesan baru",
                request_id=request_id,
            )
        self.assertNotIn(previous_message, repr(captured.exception))
        self.assertEqual(self.calls, [])

    def test_same_request_id_is_allowed_in_two_sessions(self):
        session_b = self.create_second_session()
        request_id = uuid4()
        first = self.process(self.service(), request_id=request_id)
        second = self.process(
            self.service(),
            token=TOKEN_B,
            request_id=request_id,
        )
        self.assertNotEqual(first.reply.id, second.reply.id)
        self.assertEqual(
            len(self.request_rows(self.session_a.id, request_id)),
            2,
        )
        self.assertEqual(
            len(self.request_rows(session_b.id, request_id)),
            2,
        )

    def test_request_id_never_reads_another_sessions_reply(self):
        session_b = self.create_second_session()
        request_id = uuid4()
        response_b = self.process(
            self.service(reply="Jawaban B"),
            token=TOKEN_B,
            request_id=request_id,
        )
        response_a = self.process(
            self.service(reply="Jawaban A"),
            token=TOKEN_A,
            request_id=request_id,
        )
        self.assertEqual(response_b.reply.content, "Jawaban B")
        self.assertEqual(response_a.reply.content, "Jawaban A")
        self.assertNotEqual(session_b.id, self.session_a.id)

    def test_current_session_history_is_chronological_and_isolated(self):
        self.create_second_session()
        self.process(
            self.service(reply="A1"),
            token=TOKEN_A,
            message="U1",
        )
        self.process(
            self.service(reply="B1"),
            token=TOKEN_B,
            message="U2",
        )
        current_a = self.session_service.get_current_session(
            self.db,
            TOKEN_A,
        )
        self.assertEqual(
            [(row.role, row.content) for row in current_a.messages],
            [("user", "U1"), ("assistant", "A1")],
        )

    def test_history_never_persists_system_or_provider_payload(self):
        request_id = uuid4()
        self.process(
            self.service(reply="Teks publik"),
            message="Teks pengguna",
            request_id=request_id,
        )
        rendered = repr(self.request_rows(self.session_a.id, request_id))
        self.assertNotIn("systemPrompt", rendered)
        self.assertNotIn("provider_payload", rendered)
        self.assertNotIn(TOKEN_A, rendered)

    def test_timeout_is_safe_and_leaves_only_restart_safe_user_marker(self):
        request_id = uuid4()
        with self.assertRaises(DemoChatProviderTimeoutError) as captured:
            self.process(
                self.service(error=TimeoutError("provider secret")),
                request_id=request_id,
            )
        self.assertNotIn("provider secret", repr(captured.exception))
        rows = self.request_rows(self.session_a.id, request_id)
        self.assertEqual([row.role for row in rows], ["user"])
        current = self.session_service.get_current_session(
            self.db,
            TOKEN_A,
        )
        self.assertEqual(current.messages, ())
        self.assertEqual(current.session.message_count, 0)

    def test_unknown_provider_error_is_safe_and_no_assistant_is_saved(self):
        request_id = uuid4()
        with self.assertRaises(DemoChatProviderError) as captured:
            self.process(
                self.service(error=RuntimeError("raw model marker")),
                request_id=request_id,
            )
        self.assertNotIn("raw model marker", repr(captured.exception))
        rows = self.request_rows(self.session_a.id, request_id)
        self.assertEqual([row.role for row in rows], ["user"])

    def test_empty_core_reply_is_service_unavailable(self):
        request_id = uuid4()
        with self.assertRaises(DemoChatServiceUnavailableError):
            self.process(
                self.service(reply=" \n"),
                request_id=request_id,
            )
        self.assertEqual(
            [row.role for row in self.request_rows(
                self.session_a.id,
                request_id,
            )],
            ["user"],
        )

    def test_assistant_persistence_failure_rolls_back_assistant_only(self):
        request_id = uuid4()
        service = self.service(
            repository=_AssistantFailingRepository()
        )
        with self.assertRaises(
            DemoChatServiceUnavailableError
        ) as captured:
            self.process(service, request_id=request_id)
        self.assertNotIn("unsafe database detail", str(captured.exception))
        rows = DemoChatMessageRepository().list_by_request_id(
            self.db,
            demo_session_id=self.session_a.id,
            request_id=request_id,
        )
        self.assertEqual([row.role for row in rows], ["user"])

    def test_revoked_and_expired_sessions_never_reach_core(self):
        self.session_a.revoked_at = self.now
        self.db.commit()
        with self.assertRaises(DemoSessionRequiredError):
            self.process(self.service())
        self.assertEqual(self.calls, [])

        self.session_a.revoked_at = None
        self.session_a.idle_expires_at = self.now - timedelta(seconds=1)
        self.db.commit()
        with self.assertRaises(DemoSessionRequiredError):
            self.process(self.service())
        self.assertEqual(self.calls, [])

    def test_inactive_owner_is_rejected_before_core(self):
        owner = self.db.get(Customer, self.session_a.owner_customer_id)
        owner.is_active = False
        self.db.commit()
        with self.assertRaises(DemoSessionRequiredError):
            self.process(self.service())
        self.assertEqual(self.calls, [])

    def test_session_persistence_failure_maps_to_service_unavailable(self):
        service = self.service(
            session_service=_FailingResolveSessionService()
        )
        with self.assertRaises(DemoChatServiceUnavailableError):
            self.process(service)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.lock.acquired, [])

    def test_database_lock_is_session_scoped_and_always_released(self):
        self.process(self.service())
        self.assertEqual(self.lock.acquired, [self.session_a.id])
        self.assertEqual(self.lock.released, [self.session_a.id])

    def test_cancellation_after_lock_acquire_still_releases_lock(self):
        with self.assertRaises(asyncio.CancelledError):
            self.process(
                self.service(error=asyncio.CancelledError()),
            )
        self.assertEqual(self.lock.acquired, [self.session_a.id])
        self.assertEqual(self.lock.released, [self.session_a.id])

    def test_reservation_mutation_stays_null_without_structured_core_result(self):
        response = self.process(
            self.service(
                reply="Reservasi berhasil dibuat dengan ID 999."
            )
        )
        self.assertIsNone(response.reservation_mutation)


class DemoPostgreSQLAdvisoryLockTests(unittest.TestCase):
    def setUp(self):
        self.lock = DemoPostgreSQLAdvisoryLock()
        self.session_id = 17

    def test_normal_acquire_and_unlock_closes_without_invalidation(self):
        connection = _FakeConnection(True, True)
        db = _FakeDatabaseSession(connection)
        lease = self.lock.acquire(db, demo_session_id=self.session_id)
        self.assertIsNotNone(lease)
        self.lock.release(
            db,
            demo_session_id=self.session_id,
            lease=lease,
        )
        self.assertEqual(connection.commits, 2)
        self.assertEqual(connection.invalidations, 0)
        self.assertEqual(connection.closes, 1)

    def test_acquire_false_closes_normally_without_unlock(self):
        connection = _FakeConnection(False)
        db = _FakeDatabaseSession(connection)
        self.assertIsNone(
            self.lock.acquire(db, demo_session_id=self.session_id)
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.invalidations, 0)
        self.assertEqual(connection.closes, 1)

    def test_acquire_exception_is_safe_and_closes_connection(self):
        connection = _FakeConnection(RuntimeError("connection detail"))
        db = _FakeDatabaseSession(connection)
        with self.assertRaises(DemoChatServiceUnavailableError) as captured:
            self.lock.acquire(db, demo_session_id=self.session_id)
        self.assertNotIn("connection detail", repr(captured.exception))
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.invalidations, 0)
        self.assertEqual(connection.closes, 1)

    def test_unlock_false_invalidates_and_discards_connection(self):
        connection = _FakeConnection(True, False)
        db = _FakeDatabaseSession(connection)
        lease = self.lock.acquire(db, demo_session_id=self.session_id)
        with self.assertRaises(DemoChatServiceUnavailableError):
            self.lock.release(
                db,
                demo_session_id=self.session_id,
                lease=lease,
            )
        self.assertEqual(connection.invalidations, 1)
        self.assertEqual(connection.closes, 1)

    def test_unlock_exception_invalidates_without_detail_leak(self):
        connection = _FakeConnection(
            True,
            RuntimeError("database secret"),
        )
        db = _FakeDatabaseSession(connection)
        lease = self.lock.acquire(db, demo_session_id=self.session_id)
        with self.assertRaises(DemoChatServiceUnavailableError) as captured:
            self.lock.release(
                db,
                demo_session_id=self.session_id,
                lease=lease,
            )
        self.assertNotIn("database secret", repr(captured.exception))
        self.assertEqual(connection.invalidations, 1)
        self.assertEqual(connection.closes, 1)


class DemoSimulatedHandoffServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.create_function("char_length", 1, len)

        Customer.__table__.create(self.engine)
        DemoSession.__table__.create(self.engine)
        DemoHandoffEvent.__table__.create(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.now = datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc)
        session_service = DemoSessionService(
            token_generator=lambda: TOKEN_A,
            clock=lambda: self.now,
        )
        session_service.create_session(self.db)
        self.session = self.db.scalar(select(DemoSession))
        self.memory = MemoryManager()
        self.adapter = DemoSimulatedHandoffService(
            self.memory,
            demo_session_id=self.session.id,
            reference_generator=lambda: "DEMO-HO-UNIT1",
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_explicit_handoff_is_allowlisted_and_simulated(self):
        state = self.adapter.require_handoff(
            "owner-memory-key",
            "explicit_human_request",
            db=self.db,
            owner_customer_id=self.session.owner_customer_id,
        )
        row = self.db.scalar(select(DemoHandoffEvent))
        self.assertEqual(state["reference"], "DEMO-HO-UNIT1")
        self.assertEqual(state["status"], "simulated")
        self.assertEqual(row.reference, "DEMO-HO-UNIT1")
        self.assertEqual(row.status, "simulated")
        self.assertEqual(
            row.safe_summary,
            SAFE_DEMO_HANDOFF_SUMMARIES["explicit_human_request"],
        )
        self.assertIn("disimulasikan", self.adapter.explicit_response())

    def test_unknown_handoff_reason_fails_without_event(self):
        with self.assertRaises(DemoChatServiceUnavailableError):
            self.adapter.require_handoff(
                "owner-memory-key",
                "attacker supplied summary",
                db=self.db,
                owner_customer_id=self.session.owner_customer_id,
            )
        self.assertEqual(
            self.db.scalar(
                select(func.count()).select_from(DemoHandoffEvent)
            ),
            0,
        )

    def test_handoff_recovery_is_scoped_to_demo_session(self):
        other_service = DemoSessionService(
            token_generator=lambda: TOKEN_B,
            clock=lambda: self.now,
        )
        other_service.create_session(self.db)
        other = self.db.scalar(
            select(DemoSession).where(
                DemoSession.token_digest
                == digest_demo_session_token(TOKEN_B)
            )
        )
        self.adapter.require_handoff(
            "owner-memory-key",
            "explicit_human_request",
            db=self.db,
            owner_customer_id=self.session.owner_customer_id,
        )
        other_adapter = DemoSimulatedHandoffService(
            MemoryManager(),
            demo_session_id=other.id,
        )
        self.assertIsNone(
            other_adapter.restore_active_handoff(
                "other-memory-key",
                self.db,
                other.owner_customer_id,
            )
        )

    def test_adapter_has_no_production_ticket_or_dispatcher_dependency(self):
        attributes = repr(vars(self.adapter)).casefold()
        self.assertNotIn("ticket", attributes)
        self.assertNotIn("telegram", attributes)
        self.assertNotIn("dispatcher", attributes)


if __name__ == "__main__":
    unittest.main()
