"""Real PostgreSQL safety and concurrency tests for internal demo chat."""

import asyncio
from datetime import datetime, timedelta, timezone
import os
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.agents.result import (
    AgentTurnResult,
    ReservationOperationResult,
    ReservationOperationType,
)
from app.db.models.conversation_workflow_state import (
    ConversationWorkflowState,
)
from app.db.models.customer import Customer
from app.db.models.demo_persistence import (
    DEMO_SAFE_CONTENT_VERSION,
    DemoChatMessage,
    DemoHandoffEvent,
    DemoSession,
)
from app.db.models.reservation import Reservation
from app.db.models.support_ticket import SupportTicket
from app.db.models.support_ticket_notification import (
    SupportTicketNotification,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
)
from app.core.conversation_memory import build_authenticated_memory_key
from app.integrations.telegram.owner_notification_dispatcher import (
    OwnerNotificationDispatcher,
)
from app.schemas.reservation import ReservationCreate
from app.services.demo_chat_service import (
    DemoChatRequestConflictError,
    DemoPostgreSQLAdvisoryLock,
    DemoChatService,
    DemoChatServiceUnavailableError,
)
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    digest_demo_session_token,
)
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from app.services.reservation.service import ReservationService
from migrations.add_demo_chat_request_id import (
    DemoChatRequestIdMigrationError,
    migrate as migrate_request_id,
)
from migrations.add_demo_chat_reservation_mutation import (
    migrate as migrate_reservation_mutation,
)
from app.services.demo_chat_errors import DemoHistoryResetRequiredError
from migrations.add_demo_chat_content_safety import migrate as migrate_content_safety
from migrations.add_demo_persistence import migrate as migrate_demo
from tests.integration.disposable_schema import DisposableSchemaResources


TOKEN_A = "P" * 43
TOKEN_B = "Q" * 43
SEEDED_DEMO_RESERVATION_ID = (2**30) + 104_831


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


class _ReplyCore:
    def __init__(self, calls, reply="Jawaban PostgreSQL."):
        self.calls = calls
        self.reply = reply

    async def process_turn(self, **values):
        self.calls.append(values)
        return AgentTurnResult(reply=self.reply)


class _BlockingCore:
    def __init__(
        self,
        *,
        entered,
        release,
        calls,
        create_reservation=False,
    ):
        self.entered = entered
        self.release = release
        self.calls = calls
        self.create_reservation = create_reservation

    async def process_turn(
        self,
        *,
        db,
        customer,
        session_reference,
        message,
    ):
        self.calls.append((customer.id, session_reference, message))
        reservation_operation = None
        if self.create_reservation:
            reservation = ReservationService().create_reservation(
                db,
                ReservationCreate(
                    name="Rizal",
                    people=2,
                    date="2026-08-02",
                    time="19:00",
                ),
                owner_customer_id=customer.id,
            )
            reservation_operation = ReservationOperationResult(
                operation=ReservationOperationType.CREATED,
                reference=reservation.reference,
            )
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test release timeout")
        return AgentTurnResult(
            reply="Jawaban blocking tersimpan.",
            reservation_operation=reservation_operation,
        )


class _AsyncBlockingCore:
    def __init__(self, entered, release):
        self.entered = entered
        self.release = release

    async def process_turn(self, **_values):
        self.entered.set()
        await self.release.wait()
        return AgentTurnResult(reply="Jawaban pertama.")


class _CancellingCore:
    async def process_turn(self, **_values):
        raise asyncio.CancelledError()


class _DelegatingBlockingCore:
    def __init__(self, core, entered, release):
        self.core = core
        self.entered = entered
        self.release = release

    async def process_turn(self, **values):
        response = await self.core.process_turn(**values)
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test release timeout")
        return response


class _SignalingSessionService(DemoSessionService):
    def __init__(self, *args, third_resolve, **kwargs):
        super().__init__(*args, **kwargs)
        self.third_resolve = third_resolve
        self.resolve_count = 0

    def resolve_active_session(self, *args, **kwargs):
        self.resolve_count += 1
        if self.resolve_count >= 3:
            self.third_resolve.set()
        return super().resolve_active_session(*args, **kwargs)


class _AssistantFailingRepository(DemoChatMessageRepository):
    def append_request_message(self, db, **values):
        if values["role"] == "assistant":
            raise RuntimeError("forced assistant persistence failure")
        return super().append_request_message(db, **values)


class _EngineBindOnlySession:
    def __init__(self, engine):
        self.engine = engine

    def get_bind(self):
        return self.engine


class _UnlockBeforeReleaseLock(DemoPostgreSQLAdvisoryLock):
    def __init__(self):
        self.manual_release_result = None
        self.released_lease = None

    def release(self, db, *, demo_session_id, lease):
        self.manual_release_result = lease.connection.scalar(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": lease.lock_key},
        )
        lease.connection.commit()
        self.released_lease = lease
        return super().release(
            db,
            demo_session_id=demo_session_id,
            lease=lease,
        )


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class DemoChatPostgreSQLTests(unittest.TestCase):
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

        cls.schema = f"aura_demo_chat_test_{uuid4().hex[:12]}"
        cls.resources = DisposableSchemaResources(
            admin_engine=cls.admin,
            schema=cls.schema,
            allowed_prefixes=("aura_demo_chat_test_",),
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
            pool_size=8,
            max_overflow=8,
        )
        cls.resources.track_engine(cls.engine)

        Customer.__table__.create(cls.engine)
        Reservation.__table__.create(cls.engine)
        ConversationWorkflowState.__table__.create(cls.engine)
        SupportTicket.__table__.create(cls.engine)
        SupportTicketNotification.__table__.create(cls.engine)
        migrate_demo(cls.engine, schema=cls.schema)
        if not migrate_request_id(cls.engine, schema=cls.schema):
            raise RuntimeError("Request ID migration did not apply.")
        if not migrate_reservation_mutation(cls.engine, schema=cls.schema):
            raise RuntimeError("Reservation mutation migration did not apply.")
        if not migrate_content_safety(cls.engine, schema=cls.schema):
            raise RuntimeError("Content safety migration did not apply.")
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
                    "conversation_workflow_states, reservations, "
                    "demo_handoff_events, demo_chat_messages, "
                    "demo_sessions, customers "
                    "RESTART IDENTITY CASCADE"
                )
            )
        self.now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        self.create_session(TOKEN_A)

    def create_session(self, token):
        with self.Session() as db:
            DemoSessionService(
                token_generator=lambda: token,
                clock=lambda: self.now,
            ).create_session(db)

    def new_migration_engine(self):
        # Concurrency tests may leave several healthy idle connections in the
        # shared pool. Dispose only that test pool before allocating another
        # disposable schema connection under the restricted test role.
        self.engine.dispose()
        schema = f"aura_demo_chat_test_{uuid4().hex[:12]}"
        self.resources.track_schema(schema)
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_url = make_url(
            os.environ["TEST_DATABASE_URL"]
        ).update_query_dict(
            {"options": f"-csearch_path={schema},public"}
        )
        engine = create_engine(
            schema_url,
            pool_pre_ping=True,
            poolclass=NullPool,
        )
        self.resources.track_engine(engine)
        Customer.__table__.create(engine)
        migrate_demo(engine, schema=schema)
        return schema, engine

    def new_single_connection_pool_engine(self):
        engine = create_engine(
            self.engine.url,
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
        )
        self.resources.track_engine(engine)
        self.assertEqual(type(engine.pool).__name__, "QueuePool")
        return engine

    def session_row(self, token):
        with self.Session() as db:
            return db.scalar(
                select(DemoSession).where(
                    DemoSession.token_digest
                    == digest_demo_session_token(token)
                )
            )

    def service(self, calls=None, core_factory=None, **kwargs):
        active_calls = [] if calls is None else calls
        return DemoChatService(
            session_service=kwargs.pop(
                "session_service",
                DemoSessionService(clock=lambda: self.now),
            ),
            core_factory=core_factory
            or (lambda _session_id: _ReplyCore(active_calls)),
            clock=lambda: self.now,
            **kwargs,
        )

    @staticmethod
    def run_process(service, db, token, request_id, message="Halo"):
        return asyncio.run(
            service.process(
                db,
                raw_session_token=token,
                message=message,
                request_id=request_id,
            )
        )

    def run_thread(
        self,
        *,
        service,
        token,
        request_id,
        output,
        message="Halo",
    ):
        def target():
            db = self.Session()
            try:
                output.append(
                    self.run_process(
                        service,
                        db,
                        token,
                        request_id,
                        message,
                    )
                )
            except BaseException as error:
                output.append(error)
            finally:
                db.close()

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def counts(self):
        with self.Session() as db:
            return {
                "messages": db.scalar(
                    select(func.count()).select_from(DemoChatMessage)
                ),
                "handoffs": db.scalar(
                    select(func.count()).select_from(DemoHandoffEvent)
                ),
                "reservations": db.scalar(
                    select(func.count()).select_from(Reservation)
                ),
                "tickets": db.scalar(
                    select(func.count()).select_from(SupportTicket)
                ),
                "notifications": db.scalar(
                    select(func.count()).select_from(
                        SupportTicketNotification
                    )
                ),
            }

    def test_migration_is_additive_idempotent_and_has_partial_uniqueness(self):
        self.assertFalse(
            migrate_request_id(self.engine, schema=self.schema)
        )
        inspector = inspect(self.engine)
        column = next(
            item
            for item in inspector.get_columns(
                "demo_chat_messages",
                schema=self.schema,
            )
            if item["name"] == "request_id"
        )
        self.assertTrue(column["nullable"])
        self.assertEqual(column["type"].__class__.__name__.upper(), "UUID")
        indexes = {
            item["name"]: item
            for item in inspector.get_indexes(
                "demo_chat_messages",
                schema=self.schema,
            )
        }
        for role in ("user", "assistant"):
            name = (
                "uq_demo_chat_messages_session_request_"
                f"{role}"
            )
            self.assertTrue(indexes[name]["unique"])
            self.assertEqual(
                indexes[name]["column_names"],
                ["demo_session_id", "request_id"],
            )

    def test_migration_rejects_incompatible_existing_index_safely(self):
        index = "uq_demo_chat_messages_session_request_user"
        with self.engine.begin() as connection:
            connection.execute(text(f'DROP INDEX "{self.schema}"."{index}"'))
            connection.execute(
                text(
                    f'CREATE UNIQUE INDEX "{index}" '
                    f'ON "{self.schema}"."demo_chat_messages" '
                    "(request_id, demo_session_id)"
                )
            )
        try:
            with self.assertRaises(DemoChatRequestIdMigrationError):
                migrate_request_id(self.engine, schema=self.schema)
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text(f'DROP INDEX "{self.schema}"."{index}"')
                )
                connection.execute(
                    text(
                        f'CREATE UNIQUE INDEX "{index}" '
                        f'ON "{self.schema}"."demo_chat_messages" '
                        "(demo_session_id, request_id) "
                        "WHERE role = 'user' "
                        "AND request_id IS NOT NULL"
                    )
                )

    def _assert_new_migration_rejects_wrong_predicate(self, role):
        schema, engine = self.new_migration_engine()
        self.assertTrue(migrate_request_id(engine, schema=schema))
        index = f"uq_demo_chat_messages_session_request_{role}"
        with engine.begin() as connection:
            connection.execute(text(f'DROP INDEX "{schema}"."{index}"'))
            connection.execute(
                text(
                    f'CREATE UNIQUE INDEX "{index}" '
                    f'ON "{schema}"."demo_chat_messages" '
                    "(demo_session_id, request_id) "
                    f"WHERE role = '{role}'"
                )
            )

        with self.assertRaises(
            DemoChatRequestIdMigrationError
        ) as captured:
            migrate_request_id(engine, schema=schema)

        rendered = str(captured.exception) + repr(captured.exception)
        self.assertNotIn("postgresql://", rendered.casefold())
        self.assertNotIn("password", rendered.casefold())

    def test_new_migration_rejects_wrong_user_predicate_only(self):
        self._assert_new_migration_rejects_wrong_predicate("user")

    def test_new_migration_rejects_wrong_assistant_predicate_only(self):
        self._assert_new_migration_rejects_wrong_predicate("assistant")

    def test_migration_rejects_incompatible_request_id_type_safely(self):
        schema, engine = self.new_migration_engine()
        with engine.begin() as connection:
            connection.execute(
                text(
                    f'ALTER TABLE "{schema}"."demo_chat_messages" '
                    "ADD COLUMN request_id TEXT NULL"
                )
            )
        with self.assertRaises(DemoChatRequestIdMigrationError) as captured:
            migrate_request_id(engine, schema=schema)
        self.assertEqual(
            str(captured.exception),
            "Demo chat request migration failed safely.",
        )

    def test_partial_request_migration_failure_rolls_back_all_new_ddl(self):
        schema, engine = self.new_migration_engine()
        assistant_index = (
            "uq_demo_chat_messages_session_request_assistant"
        )
        user_index = "uq_demo_chat_messages_session_request_user"
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as db:
            DemoSessionService(
                token_generator=lambda: "R" * 43,
                clock=lambda: self.now,
            ).create_session(db)
            session = db.scalar(select(DemoSession))
            session_id = session.id
        with engine.begin() as connection:
            legacy_id = connection.scalar(
                text(
                    f'INSERT INTO "{schema}"."demo_chat_messages" '
                    "(demo_session_id, role, content, created_at) "
                    "VALUES (:demo_session_id, 'user', "
                    ":content, :created_at) RETURNING id"
                ),
                {
                    "demo_session_id": session_id,
                    "content": "legacy-before-request-id",
                    "created_at": self.now,
                },
            )

        initial_inspector = inspect(engine)
        self.assertNotIn(
            "request_id",
            {
                column["name"]
                for column in initial_inspector.get_columns(
                    "demo_chat_messages",
                    schema=schema,
                )
            },
        )
        initial_index_names = {
            item.get("name")
            for item in initial_inspector.get_indexes(
                "demo_chat_messages",
                schema=schema,
            )
        }
        self.assertNotIn(user_index, initial_index_names)
        self.assertNotIn(assistant_index, initial_index_names)

        real_text = text

        def fail_second_index(statement):
            if (
                f'CREATE UNIQUE INDEX "{assistant_index}"'
                in str(statement)
            ):
                raise SQLAlchemyError("forced second-index failure")
            return real_text(statement)

        with patch(
            "migrations.add_demo_chat_request_id.text",
            side_effect=fail_second_index,
        ):
            with self.assertRaises(
                DemoChatRequestIdMigrationError
            ) as captured:
                migrate_request_id(engine, schema=schema)

        self.assertNotIn(
            "forced second-index failure",
            repr(captured.exception),
        )
        rolled_back_inspector = inspect(engine)
        self.assertNotIn(
            "request_id",
            {
                column["name"]
                for column in rolled_back_inspector.get_columns(
                    "demo_chat_messages",
                    schema=schema,
                )
            },
        )
        index_names = {
            item.get("name")
            for item in rolled_back_inspector.get_indexes(
                "demo_chat_messages",
                schema=schema,
            )
        }
        self.assertNotIn(user_index, index_names)
        self.assertNotIn(assistant_index, index_names)
        with engine.connect() as connection:
            persisted_content = connection.scalar(
                text(
                    f'SELECT content FROM "{schema}".'
                    '"demo_chat_messages" WHERE id = :message_id'
                ),
                {"message_id": legacy_id},
            )
        self.assertEqual(persisted_content, "legacy-before-request-id")

        self.assertTrue(migrate_request_id(engine, schema=schema))
        retry_inspector = inspect(engine)
        self.assertIn(
            "request_id",
            {
                column["name"]
                for column in retry_inspector.get_columns(
                    "demo_chat_messages",
                    schema=schema,
                )
            },
        )
        retry_index_names = {
            item.get("name")
            for item in retry_inspector.get_indexes(
                "demo_chat_messages",
                schema=schema,
            )
        }
        self.assertIn(user_index, retry_index_names)
        self.assertIn(assistant_index, retry_index_names)
        self.assertFalse(migrate_request_id(engine, schema=schema))

    def test_real_database_success_and_restart_safe_replay(self):
        request_id = uuid4()
        first_calls = []
        with self.Session() as db:
            first = self.run_process(
                self.service(first_calls),
                db,
                TOKEN_A,
                request_id,
            )
        restarted_calls = []
        with self.Session() as db:
            replay = self.run_process(
                self.service(restarted_calls),
                db,
                TOKEN_A,
                request_id,
            )
        self.assertEqual(first, replay)
        self.assertEqual(len(first_calls), 1)
        self.assertEqual(restarted_calls, [])
        self.assertEqual(self.counts()["messages"], 2)
        with self.Session() as db:
            rows = DemoChatMessageRepository().list_by_request_id(
                db,
                demo_session_id=self.session_row(TOKEN_A).id,
                request_id=request_id,
            )
        self.assertIsNone(rows[0].content_safety_version)
        self.assertEqual(
            rows[1].content_safety_version,
            DEMO_SAFE_CONTENT_VERSION,
        )

    def test_completed_replay_with_different_message_is_conflict(self):
        request_id = uuid4()
        with self.Session() as db:
            self.run_process(
                self.service(),
                db,
                TOKEN_A,
                request_id,
                message="Pesan pertama",
            )
        replay_calls = []
        with self.Session() as db:
            with self.assertRaises(DemoChatRequestConflictError):
                self.run_process(
                    self.service(replay_calls),
                    db,
                    TOKEN_A,
                    request_id,
                    message="Pesan berbeda",
                )
        self.assertEqual(replay_calls, [])
        self.assertEqual(self.counts()["messages"], 2)

    def test_incomplete_marker_returns_conflict_after_restart(self):
        session = self.session_row(TOKEN_A)
        request_id = uuid4()
        with self.Session() as db:
            DemoChatMessageRepository().append_request_message(
                db,
                demo_session_id=session.id,
                role="user",
                content="Incomplete",
                request_id=request_id,
                created_at=self.now,
            )
            db.commit()
        with self.Session() as db:
            with self.assertRaises(DemoChatRequestConflictError):
                self.run_process(
                    self.service(),
                    db,
                    TOKEN_A,
                    request_id,
                    message="Incomplete",
                )
        with self.Session() as db:
            current = DemoSessionService(
                clock=lambda: self.now,
            ).get_current_session(db, TOKEN_A)
        self.assertEqual(current.messages, ())
        self.assertEqual(current.session.message_count, 0)

    def test_incomplete_different_message_remains_safe_conflict(self):
        session = self.session_row(TOKEN_A)
        request_id = uuid4()
        previous_message = "internal previous marker"
        with self.Session() as db:
            DemoChatMessageRepository().append_request_message(
                db,
                demo_session_id=session.id,
                role="user",
                content=previous_message,
                request_id=request_id,
                created_at=self.now,
            )
            db.commit()
        with self.Session() as db:
            with self.assertRaises(
                DemoChatRequestConflictError
            ) as captured:
                self.run_process(
                    self.service(),
                    db,
                    TOKEN_A,
                    request_id,
                    message="different message",
                )
        self.assertNotIn(previous_message, repr(captured.exception))

    def test_database_constraint_enforces_session_role_uniqueness(self):
        session = self.session_row(TOKEN_A)
        request_id = uuid4()
        with self.Session() as db:
            repository = DemoChatMessageRepository()
            repository.append_request_message(
                db,
                demo_session_id=session.id,
                role="user",
                content="First",
                request_id=request_id,
                created_at=self.now,
            )
            db.commit()
            with self.assertRaises(IntegrityError):
                repository.append_request_message(
                    db,
                    demo_session_id=session.id,
                    role="user",
                    content="Duplicate",
                    request_id=request_id,
                    created_at=self.now,
                )
            db.rollback()

    def test_same_request_concurrent_is_processed_at_most_once(self):
        request_id = uuid4()
        entered = threading.Event()
        release = threading.Event()
        calls = []
        first_service = self.service(
            core_factory=lambda _id: _BlockingCore(
                entered=entered,
                release=release,
                calls=calls,
            )
        )
        second_service = self.service(calls=[])
        first_output = []
        second_output = []
        first = self.run_thread(
            service=first_service,
            token=TOKEN_A,
            request_id=request_id,
            output=first_output,
        )
        self.assertTrue(entered.wait(3))
        second = self.run_thread(
            service=second_service,
            token=TOKEN_A,
            request_id=request_id,
            output=second_output,
        )
        second.join(3)
        release.set()
        first.join(3)
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            any(
                isinstance(item, DemoChatRequestConflictError)
                for item in second_output
            )
        )
        self.assertEqual(self.counts()["messages"], 2)
        session = self.session_row(TOKEN_A)
        with self.Session() as db:
            rows = DemoChatMessageRepository().list_by_request_id(
                db,
                demo_session_id=session.id,
                request_id=request_id,
            )
        self.assertEqual(
            [row.content_safety_version for row in rows],
            [None, DEMO_SAFE_CONTENT_VERSION],
        )

    def test_different_requests_same_session_are_deterministically_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        first_output = []
        second_output = []
        first = self.run_thread(
            service=self.service(
                core_factory=lambda _id: _BlockingCore(
                    entered=entered,
                    release=release,
                    calls=[],
                )
            ),
            token=TOKEN_A,
            request_id=uuid4(),
            output=first_output,
        )
        self.assertTrue(entered.wait(3))
        second = self.run_thread(
            service=self.service(),
            token=TOKEN_A,
            request_id=uuid4(),
            output=second_output,
        )
        second.join(3)
        release.set()
        first.join(3)
        self.assertTrue(
            any(
                isinstance(item, DemoChatRequestConflictError)
                for item in second_output
            )
        )
        self.assertEqual(self.counts()["messages"], 2)

    def test_different_sessions_do_not_block_each_other(self):
        self.create_session(TOKEN_B)
        entered_a = threading.Event()
        entered_b = threading.Event()
        release = threading.Event()
        output_a = []
        output_b = []
        thread_a = self.run_thread(
            service=self.service(
                core_factory=lambda _id: _BlockingCore(
                    entered=entered_a,
                    release=release,
                    calls=[],
                )
            ),
            token=TOKEN_A,
            request_id=uuid4(),
            output=output_a,
        )
        self.assertTrue(entered_a.wait(3))
        thread_b = self.run_thread(
            service=self.service(
                core_factory=lambda _id: _BlockingCore(
                    entered=entered_b,
                    release=release,
                    calls=[],
                )
            ),
            token=TOKEN_B,
            request_id=uuid4(),
            output=output_b,
        )
        self.assertTrue(entered_b.wait(3))
        release.set()
        thread_a.join(3)
        thread_b.join(3)
        self.assertFalse(
            any(isinstance(item, BaseException) for item in output_a)
        )
        self.assertFalse(
            any(isinstance(item, BaseException) for item in output_b)
        )
        self.assertEqual(self.counts()["messages"], 4)

    def test_unlock_failure_discards_connection_and_pool_reuse_is_safe(self):
        engine = self.new_single_connection_pool_engine()
        db = _EngineBindOnlySession(engine)
        lock = DemoPostgreSQLAdvisoryLock()
        session_id = self.session_row(TOKEN_A).id
        failed_once = False
        invalidated_records = []

        def fail_first_unlock(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            nonlocal failed_once
            if (
                not failed_once
                and "pg_advisory_unlock" in statement.casefold()
            ):
                failed_once = True
                raise RuntimeError("forced unlock failure")

        def record_invalidation(
            _dbapi_connection,
            connection_record,
            _exception,
        ):
            invalidated_records.append(connection_record)

        lease = lock.acquire(db, demo_session_id=session_id)
        self.assertIsNotNone(lease)
        invalidated_pid = lease.connection.scalar(
            text("SELECT pg_backend_pid()")
        )
        event.listen(
            engine,
            "before_cursor_execute",
            fail_first_unlock,
        )
        event.listen(
            engine.pool,
            "invalidate",
            record_invalidation,
        )
        try:
            with self.assertRaises(DemoChatServiceUnavailableError):
                lock.release(
                    db,
                    demo_session_id=session_id,
                    lease=lease,
                )
        finally:
            event.remove(
                engine,
                "before_cursor_execute",
                fail_first_unlock,
            )
            event.remove(
                engine.pool,
                "invalidate",
                record_invalidation,
            )

        self.assertTrue(failed_once)
        self.assertEqual(len(invalidated_records), 1)
        self.assertTrue(lease.connection.closed)
        with engine.connect() as contender:
            contender_pid = contender.scalar(text("SELECT pg_backend_pid()"))
            self.assertNotEqual(contender_pid, invalidated_pid)
            self.assertIs(
                contender.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": lease.lock_key},
                ),
                True,
            )
            self.assertIs(
                contender.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lease.lock_key},
                ),
                True,
            )
            contender.commit()

        healthy_lease = lock.acquire(db, demo_session_id=session_id)
        self.assertIsNotNone(healthy_lease)
        healthy_pid = healthy_lease.connection.scalar(
            text("SELECT pg_backend_pid()")
        )
        lock.release(
            db,
            demo_session_id=session_id,
            lease=healthy_lease,
        )
        with engine.connect() as reused:
            self.assertEqual(
                reused.scalar(text("SELECT pg_backend_pid()")),
                healthy_pid,
            )

    def test_real_unlock_false_invalidates_physical_connection(self):
        engine = self.new_single_connection_pool_engine()
        db = _EngineBindOnlySession(engine)
        lock = DemoPostgreSQLAdvisoryLock()
        session_id = self.session_row(TOKEN_A).id
        invalidated_records = []

        def record_invalidation(
            _dbapi_connection,
            connection_record,
            _exception,
        ):
            invalidated_records.append(connection_record)

        lease = lock.acquire(db, demo_session_id=session_id)
        self.assertIsNotNone(lease)
        invalidated_pid = lease.connection.scalar(
            text("SELECT pg_backend_pid()")
        )

        self.assertIs(
            lease.connection.scalar(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": lease.lock_key},
            ),
            True,
        )
        lease.connection.commit()
        event.listen(
            engine.pool,
            "invalidate",
            record_invalidation,
        )
        try:
            with self.assertRaises(DemoChatServiceUnavailableError):
                lock.release(
                    db,
                    demo_session_id=session_id,
                    lease=lease,
                )
        finally:
            event.remove(
                engine.pool,
                "invalidate",
                record_invalidation,
            )

        self.assertEqual(len(invalidated_records), 1)
        self.assertTrue(lease.connection.closed)
        with engine.connect() as contender:
            contender_pid = contender.scalar(text("SELECT pg_backend_pid()"))
            self.assertNotEqual(contender_pid, invalidated_pid)
            self.assertIs(
                contender.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": lease.lock_key},
                ),
                True,
            )
            self.assertIs(
                contender.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lease.lock_key},
                ),
                True,
            )
            contender.commit()

    def test_unlock_false_does_not_mask_cancellation_or_retry_agent(self):
        request_id = uuid4()
        lock = _UnlockBeforeReleaseLock()
        calls = []

        class CountingCancellingCore:
            async def process_turn(self, **values):
                calls.append(values)
                raise asyncio.CancelledError()

        with self.Session() as db:
            with self.assertRaises(asyncio.CancelledError):
                self.run_process(
                    self.service(
                        core_factory=lambda _id: CountingCancellingCore(),
                        database_lock=lock,
                    ),
                    db,
                    TOKEN_A,
                    request_id,
                )

        self.assertIs(lock.manual_release_result, True)
        self.assertTrue(lock.released_lease.connection.closed)
        self.assertEqual(len(calls), 1)

        replay_calls = []
        with self.Session() as db:
            with self.assertRaises(DemoChatRequestConflictError):
                self.run_process(
                    self.service(replay_calls),
                    db,
                    TOKEN_A,
                    request_id,
                )
        self.assertEqual(replay_calls, [])

    def test_cancellation_releases_real_advisory_lock_for_next_request(self):
        cancelled_request = uuid4()
        with self.Session() as db:
            with self.assertRaises(asyncio.CancelledError):
                self.run_process(
                    self.service(
                        core_factory=lambda _id: _CancellingCore()
                    ),
                    db,
                    TOKEN_A,
                    cancelled_request,
                )

        with self.Session() as db:
            response = self.run_process(
                self.service(),
                db,
                TOKEN_A,
                uuid4(),
                message="request after cancellation",
            )
        self.assertEqual(response.reply.content, "Jawaban PostgreSQL.")

    def _waiting_session_revalidation(self, *, expire):
        third_resolve = threading.Event()
        session_service = _SignalingSessionService(
            clock=lambda: self.now,
            third_resolve=third_resolve,
        )

        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            core_count = 0

            def core_factory(_id):
                nonlocal core_count
                core_count += 1
                if core_count == 1:
                    return _AsyncBlockingCore(entered, release)
                return _ReplyCore([])

            service = self.service(
                session_service=session_service,
                core_factory=core_factory,
            )
            first_db = self.Session()
            second_db = self.Session()
            first = asyncio.create_task(
                service.process(
                    first_db,
                    raw_session_token=TOKEN_A,
                    message="Pertama",
                    request_id=uuid4(),
                )
            )
            await entered.wait()
            second = asyncio.create_task(
                service.process(
                    second_db,
                    raw_session_token=TOKEN_A,
                    message="Menunggu",
                    request_id=uuid4(),
                )
            )
            for _ in range(100):
                if third_resolve.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(third_resolve.is_set())
            with self.Session() as mutation_db:
                session = mutation_db.scalar(select(DemoSession))
                if expire:
                    session.idle_expires_at = self.now - timedelta(
                        seconds=1
                    )
                else:
                    session.revoked_at = self.now
                mutation_db.commit()
            release.set()
            await first
            with self.assertRaises(DemoSessionRequiredError):
                await second
            first_db.close()
            second_db.close()
            self.assertEqual(core_count, 1)

        asyncio.run(scenario())

    def test_revoked_session_is_rejected_after_waiting(self):
        self._waiting_session_revalidation(expire=False)

    def test_expired_session_is_rejected_after_waiting(self):
        self._waiting_session_revalidation(expire=True)

    def test_concurrent_duplicate_does_not_create_two_reservations(self):
        entered = threading.Event()
        release = threading.Event()
        output_a = []
        output_b = []
        request_id = uuid4()
        first = self.run_thread(
            service=self.service(
                core_factory=lambda _id: _BlockingCore(
                    entered=entered,
                    release=release,
                    calls=[],
                    create_reservation=True,
                )
            ),
            token=TOKEN_A,
            request_id=request_id,
            output=output_a,
        )
        self.assertTrue(entered.wait(3))
        second = self.run_thread(
            service=self.service(),
            token=TOKEN_A,
            request_id=request_id,
            output=output_b,
        )
        second.join(3)
        release.set()
        first.join(3)
        counts = self.counts()
        self.assertEqual(counts["reservations"], 1)
        self.assertEqual(counts["messages"], 2)
        successful = next(
            item for item in output_a if not isinstance(item, BaseException)
        )
        self.assertEqual(
            successful.reservation_mutation.operation.value,
            "created",
        )
        with self.Session() as db:
            replay = self.run_process(
                self.service(),
                db,
                TOKEN_A,
                request_id,
            )
        self.assertEqual(replay, successful)

    def test_reservation_owner_is_derived_and_cross_owner_isolation_holds(self):
        self.create_session(TOKEN_B)
        calls = []

        class _ReservationCore:
            async def process_turn(_self, *, db, customer, **_kwargs):
                result = ReservationService().create_reservation(
                    db,
                    ReservationCreate(
                        name="Rizal",
                        people=2,
                        date="2026-08-02",
                        time="19:00",
                    ),
                    owner_customer_id=customer.id,
                )
                calls.append((customer.id, result.id))
                return AgentTurnResult(reply="Reservasi tersimpan.")

        with self.Session() as db:
            response = self.run_process(
                self.service(
                    core_factory=lambda _id: _ReservationCore()
                ),
                db,
                TOKEN_A,
                uuid4(),
            )
        session_a = self.session_row(TOKEN_A)
        session_b = self.session_row(TOKEN_B)
        with self.Session() as db:
            own = ReservationService().list_recent_reservations(
                db,
                session_a.owner_customer_id,
            )
            other = ReservationService().list_recent_reservations(
                db,
                session_b.owner_customer_id,
            )
        self.assertEqual(len(own), 1)
        self.assertEqual(other, ())
        self.assertEqual(calls[0][0], session_a.owner_customer_id)
        self.assertIsNone(response.reservation_mutation)

    def test_new_shared_agent_reply_never_publishes_exact_seeded_id(self):
        demo_session = self.session_row(TOKEN_A)
        session_reference = f"demo-session-{demo_session.id}"
        memory_key = build_authenticated_memory_key(
            demo_session.owner_customer_id,
            session_reference,
        )
        with self.Session.begin() as db:
            db.add(
                ConversationWorkflowState(
                    owner_customer_id=demo_session.owner_customer_id,
                    session_reference_hash=(
                        ConversationWorkflowStateService.hash_session_reference(
                            memory_key
                        )
                    ),
                    schema_version=2,
                    payload={
                        "intent": "reservation",
                        "name": "Rizal",
                        "people": 4,
                        "date": "2026-08-01",
                        "time": "19:00",
                        "completed": False,
                        "awaiting_confirmation": True,
                        "editing_field": None,
                        "asked_fields": ["name", "people", "date", "time"],
                    },
                    is_active=True,
                    revision=1,
                )
            )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT setval("
                    "pg_get_serial_sequence('reservations', 'id'), "
                    ":seeded_id, false)"
                ),
                {"seeded_id": SEEDED_DEMO_RESERVATION_ID},
            )

        request_id = uuid4()
        service = DemoChatService(clock=lambda: self.now)
        with self.Session() as db:
            response = self.run_process(
                service,
                db,
                TOKEN_A,
                request_id,
                message="Ya",
            )
        with self.Session() as db:
            reservation = db.get(Reservation, SEEDED_DEMO_RESERVATION_ID)
            rows = DemoChatMessageRepository().list_by_request_id(
                db,
                demo_session_id=demo_session.id,
                request_id=request_id,
            )
        with self.Session() as db:
            replay = self.run_process(
                DemoChatService(clock=lambda: self.now),
                db,
                TOKEN_A,
                request_id,
                message="Ya",
            )

        assistant = next(row for row in rows if row.role == "assistant")
        user = next(row for row in rows if row.role == "user")
        boundary_text = "\n".join(
            (
                response.reply.content,
                assistant.content,
                replay.reply.content,
            )
        )
        self.assertIsNotNone(reservation)
        self.assertNotIn(str(SEEDED_DEMO_RESERVATION_ID), boundary_text)
        self.assertIn(reservation.public_reference, response.reply.content)
        self.assertIn(reservation.public_reference, assistant.content)
        self.assertEqual(replay.reply.content, response.reply.content)
        self.assertEqual(
            response.reservation_mutation.operation.value,
            "created",
        )
        self.assertEqual(
            response.reservation_mutation.reservation_reference,
            reservation.public_reference,
        )
        self.assertEqual(response.reservation_mutation, replay.reservation_mutation)
        self.assertEqual(
            (
                assistant.reservation_mutation_operation,
                assistant.reservation_mutation_reference,
            ),
            ("created", reservation.public_reference),
        )
        self.assertEqual(
            (
                user.reservation_mutation_operation,
                user.reservation_mutation_reference,
            ),
            (None, None),
        )
        self.assertNotIn(
            str(SEEDED_DEMO_RESERVATION_ID),
            "".join(
                (
                    assistant.reservation_mutation_operation,
                    assistant.reservation_mutation_reference,
                )
            ),
        )
        self.assertNotIn(
            str(SEEDED_DEMO_RESERVATION_ID),
            str(response.model_dump(by_alias=True)["reservationMutation"]),
        )

    def test_update_and_cancel_mutations_are_durable_and_replay_once(self):
        session = self.session_row(TOKEN_A)

        for offset, operation in enumerate(("update", "cancel"), start=1):
            with self.subTest(operation=operation):
                seeded_id = SEEDED_DEMO_RESERVATION_ID + (offset * 100)
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "SELECT setval("
                            "pg_get_serial_sequence('reservations', 'id'), "
                            ":seeded_id, false)"
                        ),
                        {"seeded_id": seeded_id},
                    )
                with self.Session() as db:
                    reservation = ReservationService().create_reservation(
                        db,
                        ReservationCreate(
                            name="Mutation owner",
                            people=2,
                            date=f"2026-08-0{offset + 3}",
                            time="18:00",
                        ),
                        owner_customer_id=session.owner_customer_id,
                    )
                self.assertEqual(reservation.id, seeded_id)

                core_calls = []

                class _DurableMutationCore:
                    async def process_turn(_self, *, db, customer, **_kwargs):
                        core_calls.append(operation)
                        service = ReservationService()
                        if operation == "update":
                            result = service.update_reservation_field_by_reference(
                                db,
                                reservation.reference,
                                "people",
                                4,
                                customer.id,
                            )
                            operation_type = ReservationOperationType.UPDATED
                        else:
                            result = service.cancel_reservation_by_reference(
                                db,
                                reservation.reference,
                                customer.id,
                            )
                            operation_type = ReservationOperationType.CANCELLED
                        return AgentTurnResult(
                            reply="Mutation completed safely.",
                            reservation_operation=ReservationOperationResult(
                                operation=operation_type,
                                reference=result.reference,
                            ),
                        )

                request_id = uuid4()
                with self.Session() as db:
                    original = self.run_process(
                        self.service(
                            core_factory=lambda _id: _DurableMutationCore()
                        ),
                        db,
                        TOKEN_A,
                        request_id,
                        message=f"perform {operation}",
                    )
                with self.Session() as db:
                    rows = DemoChatMessageRepository().list_by_request_id(
                        db,
                        demo_session_id=session.id,
                        request_id=request_id,
                    )
                    stored_reservation = db.get(Reservation, seeded_id)
                replay_calls = []
                with self.Session() as db:
                    replay = self.run_process(
                        self.service(replay_calls),
                        db,
                        TOKEN_A,
                        request_id,
                        message=f"perform {operation}",
                    )

                assistant = next(row for row in rows if row.role == "assistant")
                user = next(row for row in rows if row.role == "user")
                expected_operation = (
                    "cancelled" if operation == "cancel" else "updated"
                )
                self.assertEqual(core_calls, [operation])
                self.assertEqual(replay_calls, [])
                self.assertEqual(len(rows), 2)
                self.assertEqual(original, replay)
                self.assertEqual(
                    original.reservation_mutation.operation.value,
                    expected_operation,
                )
                self.assertEqual(
                    original.reservation_mutation.reservation_reference,
                    reservation.reference,
                )
                self.assertEqual(
                    (
                        assistant.reservation_mutation_operation,
                        assistant.reservation_mutation_reference,
                    ),
                    (expected_operation, reservation.reference),
                )
                self.assertEqual(
                    (
                        user.reservation_mutation_operation,
                        user.reservation_mutation_reference,
                    ),
                    (None, None),
                )
                persisted_boundary = "".join(
                    (
                        assistant.reservation_mutation_operation,
                        assistant.reservation_mutation_reference,
                    )
                )
                serialized = str(original.model_dump(by_alias=True))
                self.assertNotIn(str(seeded_id), persisted_boundary)
                self.assertNotIn(str(seeded_id), serialized)
                self.assertNotIn(str(seeded_id), str(replay.model_dump(by_alias=True)))
                if operation == "update":
                    self.assertEqual(stored_reservation.people, 4)
                else:
                    self.assertEqual(stored_reservation.status, "cancelled")

        mutation_columns = {
            item["name"]
            for item in inspect(self.engine).get_columns("demo_chat_messages")
            if item["name"].startswith("reservation_mutation_")
        }
        self.assertEqual(
            mutation_columns,
            {
                "reservation_mutation_operation",
                "reservation_mutation_reference",
            },
        )

    def test_legacy_completion_requires_reset_without_core(self):
        session = self.session_row(TOKEN_A)
        request_id = uuid4()
        repository = DemoChatMessageRepository()
        with self.Session() as db:
            repository.append_request_message(
                db,
                demo_session_id=session.id,
                role="user",
                content="legacy request",
                request_id=request_id,
                created_at=self.now,
            )
            repository.append_request_message(
                db,
                demo_session_id=session.id,
                role="assistant",
                content="legacy stored completion",
                request_id=request_id,
                created_at=self.now,
            )
            db.commit()

        replay_calls = []
        with self.Session() as db:
            with self.assertRaises(DemoHistoryResetRequiredError):
                self.run_process(
                    self.service(replay_calls),
                    db,
                    TOKEN_A,
                    request_id,
                    message="legacy request",
                )
        self.assertEqual(replay_calls, [])
        self.assertEqual(self.counts()["messages"], 2)

    def test_update_and_cancel_use_owner_filter_for_each_demo_session(self):
        self.create_session(TOKEN_B)
        session_a = self.session_row(TOKEN_A)
        session_b = self.session_row(TOKEN_B)
        with self.Session() as db:
            reservation_b = ReservationService().create_reservation(
                db,
                ReservationCreate(
                    name="Pemilik B",
                    people=2,
                    date="2026-08-03",
                    time="18:00",
                ),
                owner_customer_id=session_b.owner_customer_id,
            )

        class _MutationCore:
            def __init__(self, operation):
                self.operation = operation
                self.result = None

            async def process_turn(_self, *, db, customer, **_kwargs):
                reservation_service = ReservationService()
                if _self.operation == "update":
                    _self.result = (
                        reservation_service.update_reservation_field_by_reference(
                            db,
                            reservation_b.reference,
                            "people",
                            5,
                            customer.id,
                        )
                    )
                else:
                    _self.result = reservation_service.cancel_reservation_by_reference(
                        db,
                        reservation_b.reference,
                        customer.id,
                    )
                return AgentTurnResult(reply="Operasi selesai secara aman.")

        for operation in ("update", "cancel"):
            denied_core = _MutationCore(operation)
            with self.Session() as db:
                response = self.run_process(
                    self.service(
                        core_factory=lambda _id, core=denied_core: core
                    ),
                    db,
                    TOKEN_A,
                    uuid4(),
                )
            self.assertIsNone(denied_core.result)
            self.assertIsNone(response.reservation_mutation)

        update_core = _MutationCore("update")
        cancel_core = _MutationCore("cancel")
        with self.Session() as db:
            self.run_process(
                self.service(
                    core_factory=lambda _id: update_core
                ),
                db,
                TOKEN_B,
                uuid4(),
            )
        self.assertEqual(update_core.result.people, 5)
        with self.Session() as db:
            self.run_process(
                self.service(
                    core_factory=lambda _id: cancel_core
                ),
                db,
                TOKEN_B,
                uuid4(),
            )
        self.assertEqual(cancel_core.result.status, "cancelled")
        with self.Session() as db:
            row = db.get(Reservation, reservation_b.id)
            self.assertEqual(row.owner_customer_id, session_b.owner_customer_id)
            self.assertNotEqual(row.owner_customer_id, session_a.owner_customer_id)

    def test_simulated_handoff_creates_no_ticket_outbox_or_telegram_call(self):
        request_id = uuid4()
        with patch.object(
            OwnerNotificationDispatcher,
            "run",
            autospec=True,
        ) as dispatcher:
            with self.Session() as db:
                response = self.run_process(
                    DemoChatService(clock=lambda: self.now),
                    db,
                    TOKEN_A,
                    request_id,
                    "hubungkan saya ke admin",
                )
            with self.Session() as db:
                replay = self.run_process(
                    DemoChatService(clock=lambda: self.now),
                    db,
                    TOKEN_A,
                    request_id,
                    "hubungkan saya ke admin",
                )
        counts = self.counts()
        self.assertIsNotNone(response.handoff)
        self.assertTrue(response.handoff.reference.startswith("DEMO-HO-"))
        self.assertEqual(response.handoff.status, "simulated")
        self.assertEqual(response, replay)
        self.assertEqual(counts["handoffs"], 1)
        self.assertEqual(counts["tickets"], 0)
        self.assertEqual(counts["notifications"], 0)
        dispatcher.assert_not_called()

    def test_concurrent_duplicate_handoff_creates_only_one_demo_event(self):
        request_id = uuid4()
        entered = threading.Event()
        release = threading.Event()
        first_output = []
        second_output = []
        base = DemoChatService(clock=lambda: self.now)
        first_service = DemoChatService(
            clock=lambda: self.now,
            core_factory=lambda session_id: _DelegatingBlockingCore(
                base._build_core(session_id),
                entered,
                release,
            ),
        )

        with patch.object(
            OwnerNotificationDispatcher,
            "run",
            autospec=True,
        ) as dispatcher:
            first = self.run_thread(
                service=first_service,
                token=TOKEN_A,
                request_id=request_id,
                output=first_output,
                message="hubungkan saya ke admin",
            )
            self.assertTrue(entered.wait(3))
            second = self.run_thread(
                service=DemoChatService(clock=lambda: self.now),
                token=TOKEN_A,
                request_id=request_id,
                output=second_output,
                message="hubungkan saya ke admin",
            )
            second.join(3)
            release.set()
            first.join(5)
        self.assertEqual(self.counts()["handoffs"], 1)
        self.assertEqual(self.counts()["tickets"], 0)
        self.assertEqual(self.counts()["notifications"], 0)
        self.assertFalse(
            any(isinstance(item, BaseException) for item in first_output)
        )
        self.assertTrue(
            any(
                isinstance(item, DemoChatRequestConflictError)
                for item in second_output
            )
        )
        dispatcher.assert_not_called()

    def test_assistant_failure_rolls_back_without_success_reply(self):
        request_id = uuid4()
        with self.Session() as db:
            with self.assertRaises(DemoChatServiceUnavailableError):
                self.run_process(
                    self.service(
                        message_repository=_AssistantFailingRepository()
                    ),
                    db,
                    TOKEN_A,
                    request_id,
                )
        session = self.session_row(TOKEN_A)
        with self.Session() as db:
            rows = DemoChatMessageRepository().list_by_request_id(
                db,
                demo_session_id=session.id,
                request_id=request_id,
            )
        self.assertEqual([row.role for row in rows], ["user"])


if __name__ == "__main__":
    unittest.main()
