"""Real PostgreSQL ownership, rollback, and lock tests for demo reset."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.agents.result import AgentTurnResult
from app.agents.reservation_agent import ReservationAgent
from app.api.internal_demo_chat import get_demo_chat_service
from app.api.internal_demo_dependencies import (
    get_demo_rate_limit_service,
    get_demo_session_service,
)
from app.api.internal_demo_reservation_reset import (
    get_demo_reservation_reset_service,
)
from app.core.config import get_demo_settings
from app.core.conversation_lock_manager import ConversationLockManager
from app.core.conversation_memory import build_authenticated_memory_key
from app.db.database import get_db
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
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    demo_session_rate_limit_subject,
)
from app.db.repositories.reservation_repository import ReservationRepository
from app.integrations.telegram.owner_notification_dispatcher import (
    OwnerNotificationDispatcher,
)
from app.main import create_app
from app.schemas.reservation import ReservationCreate
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from app.services.demo_chat_errors import (
    DemoChatRequestConflictError,
    DemoChatServiceUnavailableError,
    DemoHistoryResetRequiredError,
)
from app.services.demo_chat_service import (
    DemoChatService,
    DemoPostgreSQLAdvisoryLock,
)
from app.services.demo_reservation_reset_service import (
    DemoReservationResetService,
)
from app.services.demo_rate_limit_service import DemoRateLimitService
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    digest_demo_session_token,
)
from app.services.reservation.service import ReservationService
from app.services.handoff.notification_outbox_service import (
    NotificationOutboxService,
)
from app.services.handoff.service import HandoffService
from migrations.add_demo_chat_request_id import migrate as migrate_request_id
from migrations.add_demo_chat_reservation_mutation import (
    migrate as migrate_reservation_mutation,
)
from migrations.add_demo_chat_content_safety import migrate as migrate_content_safety
from migrations.add_demo_persistence import migrate as migrate_demo
from tests.integration.disposable_schema import DisposableSchemaResources


TOKEN_A = "V" * 43
TOKEN_B = "W" * 43
TOKEN_C = "X" * 43
SERVICE_TOKEN = "safe-bff-service-token-for-reset-integration-tests"
CLIENT_SUBJECT = "d" * 64
SEEDED_DEMO_LIST_RESERVATION_ID = (2**30) + 205_771


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


class _FailAfterMessageDelete(DemoChatMessageRepository):
    def delete_by_demo_session(self, db, *, demo_session_id):
        super().delete_by_demo_session(
            db,
            demo_session_id=demo_session_id,
        )
        raise RuntimeError("forced rollback")


class _ReplyCore:
    def __init__(self):
        self.calls = 0

    async def process_turn(self, **_values):
        self.calls += 1
        return AgentTurnResult(reply="Jawaban aman.")


class _BlockingReplyCore(_ReplyCore):
    def __init__(self, entered: Event, release: Event):
        super().__init__()
        self.entered = entered
        self.release = release

    async def process_turn(self, **_values):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("Timed out waiting to release demo chat core.")
        return AgentTurnResult(reply="Jawaban aman.")


class _PausingAdvisoryLock(DemoPostgreSQLAdvisoryLock):
    def __init__(self, acquired: Event, release: Event):
        self.acquired = acquired
        self.release_event = release

    def acquire(self, db, *, demo_session_id):
        lease = super().acquire(db, demo_session_id=demo_session_id)
        if lease is not None:
            self.acquired.set()
            if not self.release_event.wait(timeout=5):
                super().release(
                    db,
                    demo_session_id=demo_session_id,
                    lease=lease,
                )
                raise RuntimeError("Timed out waiting to continue demo reset.")
        return lease


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class DemoReservationResetPostgreSQLTests(unittest.TestCase):
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

        cls.schema = f"aura_demo_reset_test_{uuid4().hex[:12]}"
        cls.resources = DisposableSchemaResources(
            admin_engine=cls.admin,
            schema=cls.schema,
            allowed_prefixes=("aura_demo_reset_test_",),
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
        TelegramIdentity.__table__.create(cls.engine)
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
                    "telegram_identities, "
                    "conversation_workflow_states, reservations, "
                    "demo_rate_limit_buckets, demo_handoff_events, "
                    "demo_chat_messages, demo_sessions, customers "
                    "RESTART IDENTITY CASCADE"
                )
            )
        self.now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        self.create_session(TOKEN_A)
        self.create_session(TOKEN_B)

    def create_session(self, token):
        with self.Session() as db:
            DemoSessionService(
                token_generator=lambda: token,
                clock=lambda: self.now,
            ).create_session(db)

    def session_row(self, db, token):
        return db.scalar(
            select(DemoSession).where(
                DemoSession.token_digest
                == digest_demo_session_token(token)
            )
        )

    def service(self, **kwargs):
        return DemoReservationResetService(
            session_service=kwargs.pop(
                "session_service",
                DemoSessionService(clock=lambda: self.now),
            ),
            clock=lambda: self.now,
            **kwargs,
        )

    @contextmanager
    def api_client(
        self,
        *,
        session_service=None,
        chat_service=None,
        reset_service=None,
        rate_limit_service=None,
    ):
        app = create_app(
            SimpleNamespace(
                APP_ENV="demo",
                APP_NAME="AURA",
                VERSION="test",
            )
        )
        app.dependency_overrides[get_demo_settings] = lambda: (
            SimpleNamespace(
                APP_ENV="demo",
                DEMO_BFF_SERVICE_TOKEN=SecretStr(SERVICE_TOKEN),
            )
        )

        def provide_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = provide_db
        if session_service is not None:
            app.dependency_overrides[
                get_demo_session_service
            ] = lambda: session_service
        if chat_service is not None:
            app.dependency_overrides[
                get_demo_chat_service
            ] = lambda: chat_service
        if reset_service is not None:
            app.dependency_overrides[
                get_demo_reservation_reset_service
            ] = lambda: reset_service
        if rate_limit_service is not None:
            app.dependency_overrides[
                get_demo_rate_limit_service
            ] = lambda: rate_limit_service
        client = TestClient(app)
        try:
            yield client
        finally:
            app.dependency_overrides.clear()
            client.close()

    @staticmethod
    def headers(token):
        return {
            "X-BFF-Service-Token": SERVICE_TOKEN,
            "X-Demo-Session-Token": token,
            "X-Demo-Client-Subject": CLIENT_SUBJECT,
        }

    def database_snapshot(self):
        with self.Session() as db:
            return {
                "customers": [
                    tuple(row)
                    for row in db.execute(
                        select(
                            Customer.id,
                            Customer.token_version,
                            Customer.is_active,
                            Customer.created_at,
                        ).order_by(Customer.id)
                    )
                ],
                "sessions": [
                    tuple(row)
                    for row in db.execute(
                        select(
                            DemoSession.id,
                            DemoSession.owner_customer_id,
                            DemoSession.token_digest,
                            DemoSession.environment_scope,
                            DemoSession.last_seen_at,
                            DemoSession.idle_expires_at,
                            DemoSession.absolute_expires_at,
                            DemoSession.revoked_at,
                        ).order_by(DemoSession.id)
                    )
                ],
                "messages": [
                    tuple(row)
                    for row in db.execute(
                        select(
                            DemoChatMessage.id,
                            DemoChatMessage.demo_session_id,
                            DemoChatMessage.role,
                            DemoChatMessage.content,
                            DemoChatMessage.request_id,
                            DemoChatMessage.created_at,
                        ).order_by(DemoChatMessage.id)
                    )
                ],
                "workflows": [
                    tuple(row)
                    for row in db.execute(
                        select(
                            ConversationWorkflowState.id,
                            ConversationWorkflowState.owner_customer_id,
                            ConversationWorkflowState.session_reference_hash,
                            ConversationWorkflowState.payload,
                            ConversationWorkflowState.is_active,
                            ConversationWorkflowState.revision,
                        ).order_by(ConversationWorkflowState.id)
                    )
                ],
                "handoffs": [
                    tuple(row)
                    for row in db.execute(
                        select(
                            DemoHandoffEvent.id,
                            DemoHandoffEvent.demo_session_id,
                            DemoHandoffEvent.reference,
                            DemoHandoffEvent.status,
                            DemoHandoffEvent.reason_code,
                        ).order_by(DemoHandoffEvent.id)
                    )
                ],
                "reservations": [
                    tuple(row)
                    for row in db.execute(
                        select(
                            Reservation.id,
                            Reservation.owner_customer_id,
                            Reservation.name,
                            Reservation.people,
                            Reservation.date,
                            Reservation.time,
                            Reservation.status,
                        ).order_by(Reservation.id)
                    )
                ],
                "buckets": [
                    tuple(row)
                    for row in db.execute(
                        select(
                            DemoRateLimitBucket.id,
                            DemoRateLimitBucket.scope_type,
                            DemoRateLimitBucket.subject_digest,
                            DemoRateLimitBucket.action,
                            DemoRateLimitBucket.request_count,
                            DemoRateLimitBucket.expires_at,
                        ).order_by(DemoRateLimitBucket.id)
                    )
                ],
            }

    @staticmethod
    def run_in_thread(target):
        result = {}

        def runner():
            try:
                result["value"] = target()
            except BaseException as error:
                result["error"] = error

        thread = Thread(target=runner, daemon=True)
        thread.start()
        return thread, result

    def seed_owner_data(self, db, token, *, other=False):
        session = self.session_row(db, token)
        owner = db.get(Customer, session.owner_customer_id)
        reservation = ReservationService(
            clock=lambda: self.now
        ).create_reservation(
            db,
            ReservationCreate(
                name="Other" if other else "Rizal",
                people=2,
                date="2026-08-03" if other else "2026-08-02",
                time="20:00" if other else "19:00",
            ),
            owner_customer_id=owner.id,
        )
        request_id = uuid4()
        db.add_all(
            [
                DemoChatMessage(
                    demo_session_id=session.id,
                    role="user",
                    content="legacy",
                    created_at=self.now - timedelta(minutes=1),
                ),
                DemoChatMessage(
                    demo_session_id=session.id,
                    role="user",
                    content="completed",
                    request_id=request_id,
                    created_at=self.now,
                ),
                DemoChatMessage(
                    demo_session_id=session.id,
                    role="assistant",
                    content="reply",
                    request_id=request_id,
                    reservation_mutation_operation="created",
                    reservation_mutation_reference=reservation.reference,
                    created_at=self.now,
                ),
                DemoChatMessage(
                    demo_session_id=session.id,
                    role="user",
                    content="incomplete",
                    request_id=uuid4(),
                    created_at=self.now,
                ),
                DemoHandoffEvent(
                    demo_session_id=session.id,
                    reference=f"DEMO-HO-{'B' if other else 'A'}",
                    status="simulated",
                    reason_code="explicit_human_request",
                    safe_summary=(
                        "Demo visitor requested simulated human assistance."
                    ),
                    created_at=self.now,
                ),
                ConversationWorkflowState(
                    owner_customer_id=owner.id,
                    session_reference_hash=(
                        ConversationWorkflowStateService.hash_session_reference(
                            build_authenticated_memory_key(
                                owner.id,
                                f"demo-session-{session.id}",
                            )
                        )
                    ),
                    schema_version=1,
                    payload={"intent": "reservation"},
                    is_active=True,
                    revision=1,
                    created_at=self.now,
                    updated_at=self.now,
                ),
                DemoRateLimitBucket(
                    scope_type="session",
                    subject_digest=demo_session_rate_limit_subject(
                        session.token_digest
                    ),
                    action="chat",
                    window_started_at=self.now,
                    window_seconds=60,
                    request_count=2,
                    expires_at=self.now + timedelta(minutes=1),
                    updated_at=self.now,
                ),
            ]
        )
        db.commit()
        return session.id, owner.id, reservation.id

    def test_owner_read_limit_count_order_and_cross_owner_isolation(self):
        with self.Session() as db:
            session_a = self.session_row(db, TOKEN_A)
            owner_a = db.get(Customer, session_a.owner_customer_id)
            session_b = self.session_row(db, TOKEN_B)
            owner_b = db.get(Customer, session_b.owner_customer_id)
            db.execute(
                text(
                    "SELECT setval("
                    "pg_get_serial_sequence('reservations', 'id'), "
                    ":seeded_id, false)"
                ),
                {"seeded_id": SEEDED_DEMO_LIST_RESERVATION_ID},
            )
            for index in range(55):
                db.add(
                    Reservation(
                        name="A",
                        people=2,
                        date=f"2026-08-{(index % 9) + 1:02d}",
                        time=f"{(index % 5) + 10:02d}:00",
                        owner_customer_id=owner_a.id,
                        public_reference=f"RSV_{index + 1:032x}",
                        status="pending",
                    )
                )
            foreign = Reservation(
                name="B",
                people=3,
                date="2026-01-01",
                time="09:00",
                owner_customer_id=owner_b.id,
                public_reference="RSV_" + ("f" * 32),
                status="pending",
            )
            db.add(foreign)
            db.commit()
            expected_rows = ReservationRepository().list_for_owner(
                db,
                owner_a.id,
                limit=50,
            )

            result = self.service().list_reservations(
                db,
                raw_session_token=TOKEN_A,
            )

            self.assertEqual(result.count, 55)
            self.assertEqual(len(result.reservations), 50)
            serialized = result.model_dump_json(by_alias=True)
            self.assertNotIn(str(SEEDED_DEMO_LIST_RESERVATION_ID), serialized)
            for item in result.model_dump(by_alias=True)["reservations"]:
                self.assertEqual(
                    set(item),
                    {
                        "reservationReference",
                        "status",
                        "reservationDate",
                        "reservationTime",
                        "partySize",
                    },
                )
                self.assertNotIn("id", item)
                self.assertNotIn("reservationId", item)
            self.assertNotIn(
                foreign.date,
                {row.reservation_date for row in result.reservations},
            )
            self.assertEqual(
                [
                    (
                        row.status,
                        row.reservation_date.isoformat(),
                        row.reservation_time.strftime("%H:%M"),
                        row.party_size,
                    )
                    for row in result.reservations
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

    def test_reset_deletes_only_session_owner_and_preserves_token_expiry(self):
        with self.Session() as db:
            session_a_id, owner_a_id, _ = self.seed_owner_data(db, TOKEN_A)
            # Include newly supported partial-date state in the real reset path.
            workflow_row = db.scalar(select(ConversationWorkflowState).where(
                ConversationWorkflowState.owner_customer_id == owner_a_id))
            workflow_row.payload = {
                "intent": "reservation", "name": "Dani", "people": 5,
                "date": None, "time": None, "completed": False,
                "awaiting_confirmation": False, "editing_field": None,
                "asked_fields": ["name", "people", "date"], "pending_reservation_day": 5,
            }
            db.commit()
            session_b_id, owner_b_id, _ = self.seed_owner_data(
                db,
                TOKEN_B,
                other=True,
            )
            db.add_all(
                [
                    DemoRateLimitBucket(
                        scope_type="ip",
                        subject_digest="a" * 64,
                        action="chat",
                        window_started_at=self.now,
                        window_seconds=60,
                        request_count=4,
                        expires_at=self.now + timedelta(minutes=1),
                        updated_at=self.now,
                    ),
                    DemoRateLimitBucket(
                        scope_type="global",
                        subject_digest="b" * 64,
                        action="chat",
                        window_started_at=self.now,
                        window_seconds=60,
                        request_count=5,
                        expires_at=self.now + timedelta(minutes=1),
                        updated_at=self.now,
                    ),
                ]
            )
            db.commit()
            original_absolute = self.session_row(
                db,
                TOKEN_A,
            ).absolute_expires_at
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_a_id,
                        DemoChatMessage.reservation_mutation_operation.is_not(None),
                    )
                ),
                1,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(ConversationWorkflowState)
                    .where(
                        ConversationWorkflowState.owner_customer_id
                        == owner_a_id
                    )
                ),
                1,
            )

            first = asyncio.run(
                self.service().reset(db, raw_session_token=TOKEN_A)
            )
            second = asyncio.run(
                self.service().reset(db, raw_session_token=TOKEN_A)
            )

            self.assertEqual(first.session.message_count, 0)
            self.assertEqual(second.status, "reset")
            retained = self.session_row(db, TOKEN_A)
            self.assertIsNotNone(retained)
            self.assertEqual(retained.absolute_expires_at, original_absolute)
            self.assertIsNotNone(db.get(Customer, owner_a_id))
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(DemoChatMessage.demo_session_id == session_a_id)
                ),
                0,
            )

            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_a_id,
                        DemoChatMessage.reservation_mutation_operation.is_not(None),
                    )
                ),
                0,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(Reservation)
                    .where(Reservation.owner_customer_id == owner_a_id)
                ),
                0,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(ConversationWorkflowState)
                    .where(
                        ConversationWorkflowState.owner_customer_id
                        == owner_a_id
                    )
                ),
                0,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(DemoChatMessage.demo_session_id == session_b_id)
                ),
                4,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_b_id,
                        DemoChatMessage.reservation_mutation_operation.is_not(None),
                    )
                ),
                1,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(Reservation)
                    .where(Reservation.owner_customer_id == owner_b_id)
                ),
                1,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(ConversationWorkflowState)
                    .where(
                        ConversationWorkflowState.owner_customer_id
                        == owner_b_id
                    )
                ),
                1,
            )
            scopes = set(db.scalars(select(DemoRateLimitBucket.scope_type)))
            self.assertEqual(scopes, {"session", "ip", "global"})
            retained_session_bucket_count = db.scalar(
                select(func.count())
                .select_from(DemoRateLimitBucket)
                .where(
                    DemoRateLimitBucket.scope_type == "session",
                    DemoRateLimitBucket.subject_digest
                    == demo_session_rate_limit_subject(
                        retained.token_digest
                    ),
                )
            )
            self.assertEqual(retained_session_bucket_count, 1)
            current = self.service().list_reservations(
                db,
                raw_session_token=TOKEN_A,
            )
            self.assertEqual(current.count, 0)

    def test_reset_is_the_recovery_path_for_legacy_unmarked_history(self):
        with self.Session() as db:
            session = self.session_row(db, TOKEN_A)
            db.add(
                DemoChatMessage(
                    demo_session_id=session.id,
                    role="assistant",
                    content="legacy-unmarked-assistant",
                    created_at=self.now,
                )
            )
            db.commit()
            session_service = DemoSessionService(clock=lambda: self.now)
            with self.assertRaises(DemoHistoryResetRequiredError):
                session_service.get_current_session(db, TOKEN_A)

            reset = asyncio.run(
                self.service().reset(db, raw_session_token=TOKEN_A)
            )
            current = session_service.get_current_session(db, TOKEN_A)

        self.assertEqual(reset.status, "reset")
        self.assertEqual(current.session.message_count, 0)
        self.assertEqual(current.messages, ())

    def test_real_postgresql_failure_rolls_back_all_partial_deletes(self):
        with self.Session() as db:
            session_a_id, owner_a_id, _ = self.seed_owner_data(
                db,
                TOKEN_A,
            )
            session_b_id, owner_b_id, _ = self.seed_owner_data(
                db,
                TOKEN_B,
                other=True,
            )
            db.add_all(
                [
                    DemoRateLimitBucket(
                        scope_type="ip",
                        subject_digest="a" * 64,
                        action="chat",
                        window_started_at=self.now,
                        window_seconds=60,
                        request_count=4,
                        expires_at=self.now + timedelta(minutes=1),
                        updated_at=self.now,
                    ),
                    DemoRateLimitBucket(
                        scope_type="global",
                        subject_digest="b" * 64,
                        action="chat",
                        window_started_at=self.now,
                        window_seconds=60,
                        request_count=5,
                        expires_at=self.now + timedelta(minutes=1),
                        updated_at=self.now,
                    ),
                ]
            )
            db.commit()
            before = self.database_snapshot()
            service = self.service(
                message_repository=_FailAfterMessageDelete(),
            )
            with self.assertRaises(DemoChatServiceUnavailableError):
                asyncio.run(service.reset(db, raw_session_token=TOKEN_A))

        after = self.database_snapshot()
        self.assertEqual(after, before)
        with self.Session() as verify_db:
            self.assertEqual(
                verify_db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_a_id
                    )
                ),
                4,
            )
            self.assertEqual(
                verify_db.scalar(
                    select(func.count())
                    .select_from(DemoHandoffEvent)
                    .where(
                        DemoHandoffEvent.demo_session_id == session_a_id
                    )
                ),
                1,
            )
            self.assertEqual(
                verify_db.scalar(
                    select(func.count())
                    .select_from(ConversationWorkflowState)
                    .where(
                        ConversationWorkflowState.owner_customer_id
                        == owner_a_id
                    )
                ),
                1,
            )
            self.assertEqual(
                verify_db.scalar(
                    select(func.count())
                    .select_from(Reservation)
                    .where(
                        Reservation.owner_customer_id == owner_a_id
                    )
                ),
                1,
            )
            self.assertEqual(
                verify_db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_b_id
                    )
                ),
                4,
            )
            self.assertEqual(
                verify_db.scalar(
                    select(func.count())
                    .select_from(Reservation)
                    .where(
                        Reservation.owner_customer_id == owner_b_id
                    )
                ),
                1,
            )
            self.assertIsNotNone(verify_db.get(DemoSession, session_a_id))
            self.assertIsNotNone(verify_db.get(DemoSession, session_b_id))
            self.assertIsNotNone(verify_db.get(Customer, owner_a_id))
            self.assertIsNotNone(verify_db.get(Customer, owner_b_id))
            self.assertEqual(
                set(
                    verify_db.scalars(
                        select(DemoRateLimitBucket.scope_type)
                    )
                ),
                {"session", "ip", "global"},
            )

    def test_full_chat_wins_then_reset_clears_completed_state(self):
        with self.Session() as seed_db:
            session_a_id, owner_a_id, _ = self.seed_owner_data(
                seed_db,
                TOKEN_A,
            )
            self.seed_owner_data(seed_db, TOKEN_B, other=True)

        entered = Event()
        release = Event()
        core = _BlockingReplyCore(entered, release)
        request_id = uuid4()
        chat_service = DemoChatService(
            session_service=DemoSessionService(clock=lambda: self.now),
            lock_manager=ConversationLockManager(),
            database_lock=DemoPostgreSQLAdvisoryLock(),
            core_factory=lambda _session_id: core,
            clock=lambda: self.now,
        )

        def run_chat():
            with self.Session() as chat_db:
                return asyncio.run(
                    chat_service.process(
                        chat_db,
                        raw_session_token=TOKEN_A,
                        message="Halo",
                        request_id=request_id,
                    )
                )

        thread, outcome = self.run_in_thread(run_chat)
        self.assertTrue(entered.wait(timeout=5))
        try:
            with self.Session() as reset_db:
                with self.assertRaises(DemoChatRequestConflictError):
                    asyncio.run(
                        self.service(
                            lock_manager=ConversationLockManager(),
                        ).reset(
                            reset_db,
                            raw_session_token=TOKEN_A,
                        )
                    )
            with self.Session() as session_b_db:
                independent = self.service().list_reservations(
                    session_b_db,
                    raw_session_token=TOKEN_B,
                )
                self.assertEqual(independent.count, 1)
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        if "error" in outcome:
            raise AssertionError("Chat thread failed.") from outcome["error"]
        self.assertEqual(core.calls, 1)

        with self.Session() as verify_chat_db:
            pair = list(
                verify_chat_db.scalars(
                    select(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_a_id,
                        DemoChatMessage.request_id == request_id,
                    )
                    .order_by(DemoChatMessage.id)
                )
            )
            self.assertEqual(
                [message.role for message in pair],
                ["user", "assistant"],
            )

        with self.Session() as reset_db:
            first = asyncio.run(
                self.service().reset(
                    reset_db,
                    raw_session_token=TOKEN_A,
                )
            )
            second = asyncio.run(
                self.service().reset(
                    reset_db,
                    raw_session_token=TOKEN_A,
                )
            )
            self.assertEqual(first.status, "reset")
            self.assertEqual(second.status, "reset")

        with self.Session() as verify_reset_db:
            self.assertEqual(
                verify_reset_db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_a_id
                    )
                ),
                0,
            )
            self.assertEqual(
                verify_reset_db.scalar(
                    select(func.count())
                    .select_from(Reservation)
                    .where(
                        Reservation.owner_customer_id == owner_a_id
                    )
                ),
                0,
            )
            self.assertEqual(
                verify_reset_db.scalar(
                    select(func.count())
                    .select_from(DemoHandoffEvent)
                    .where(
                        DemoHandoffEvent.demo_session_id == session_a_id
                    )
                ),
                0,
            )
            self.assertIsNotNone(
                verify_reset_db.get(DemoSession, session_a_id)
            )

    def test_full_reset_wins_blocks_chat_and_next_chat_uses_empty_state(self):
        with self.Session() as seed_db:
            session_a_id, owner_a_id, _ = self.seed_owner_data(
                seed_db,
                TOKEN_A,
            )
            self.seed_owner_data(seed_db, TOKEN_B, other=True)

        acquired = Event()
        release = Event()
        reset_service = self.service(
            lock_manager=ConversationLockManager(),
            database_lock=_PausingAdvisoryLock(acquired, release),
        )

        def run_reset():
            with self.Session() as reset_db:
                return asyncio.run(
                    reset_service.reset(
                        reset_db,
                        raw_session_token=TOKEN_A,
                    )
                )

        thread, outcome = self.run_in_thread(run_reset)
        self.assertTrue(acquired.wait(timeout=5))
        core = _ReplyCore()
        chat_service = DemoChatService(
            session_service=DemoSessionService(clock=lambda: self.now),
            lock_manager=ConversationLockManager(),
            database_lock=DemoPostgreSQLAdvisoryLock(),
            core_factory=lambda _session_id: core,
            clock=lambda: self.now,
        )
        try:
            with self.Session() as chat_db:
                with self.assertRaises(DemoChatRequestConflictError):
                    asyncio.run(
                        chat_service.process(
                            chat_db,
                            raw_session_token=TOKEN_A,
                            message="Tidak boleh bersamaan",
                            request_id=uuid4(),
                        )
                    )
            self.assertEqual(core.calls, 0)
            with self.Session() as session_b_db:
                independent = self.service().list_reservations(
                    session_b_db,
                    raw_session_token=TOKEN_B,
                )
                self.assertEqual(independent.count, 1)
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        if "error" in outcome:
            raise AssertionError("Reset thread failed.") from outcome["error"]
        self.assertEqual(outcome["value"].status, "reset")

        with self.Session() as empty_db:
            self.assertEqual(
                empty_db.scalar(
                    select(func.count())
                    .select_from(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_a_id
                    )
                ),
                0,
            )
            self.assertEqual(
                empty_db.scalar(
                    select(func.count())
                    .select_from(Reservation)
                    .where(
                        Reservation.owner_customer_id == owner_a_id
                    )
                ),
                0,
            )

        next_request_id = uuid4()
        with self.Session() as chat_db:
            response = asyncio.run(
                chat_service.process(
                    chat_db,
                    raw_session_token=TOKEN_A,
                    message="Mulai lagi",
                    request_id=next_request_id,
                )
            )
            self.assertEqual(response.reply.role, "assistant")
        self.assertEqual(core.calls, 1)

        with self.Session() as final_db:
            rows = list(
                final_db.scalars(
                    select(DemoChatMessage)
                    .where(
                        DemoChatMessage.demo_session_id == session_a_id
                    )
                    .order_by(DemoChatMessage.id)
                )
            )
            self.assertEqual(
                [(row.role, row.request_id) for row in rows],
                [
                    ("user", next_request_id),
                    ("assistant", next_request_id),
                ],
            )
            self.assertEqual(
                final_db.scalar(
                    select(func.count())
                    .select_from(Reservation)
                    .where(
                        Reservation.owner_customer_id == owner_a_id
                    )
                ),
                0,
            )
            self.assertIsNotNone(final_db.get(DemoSession, session_a_id))

    def test_chat_held_advisory_lock_makes_reset_conflict(self):
        with self.Session() as lock_db, self.Session() as reset_db:
            session_id = self.session_row(lock_db, TOKEN_A).id
            lock = DemoPostgreSQLAdvisoryLock()
            lease = lock.acquire(lock_db, demo_session_id=session_id)
            self.assertIsNotNone(lease)
            try:
                with self.assertRaises(DemoChatRequestConflictError):
                    asyncio.run(
                        self.service().reset(
                            reset_db,
                            raw_session_token=TOKEN_A,
                        )
                    )
            finally:
                lock.release(
                    lock_db,
                    demo_session_id=session_id,
                    lease=lease,
                )

    def test_reset_lock_key_blocks_chat_but_not_other_session(self):
        with self.Session() as lock_db, self.Session() as chat_db:
            session_a_id = self.session_row(lock_db, TOKEN_A).id
            lock = DemoPostgreSQLAdvisoryLock()
            lease = lock.acquire(lock_db, demo_session_id=session_a_id)
            self.assertIsNotNone(lease)
            try:
                chat = DemoChatService(
                    session_service=DemoSessionService(clock=lambda: self.now),
                    core_factory=lambda _session_id: _ReplyCore(),
                    clock=lambda: self.now,
                )
                with self.assertRaises(DemoChatRequestConflictError):
                    asyncio.run(
                        chat.process(
                            chat_db,
                            raw_session_token=TOKEN_A,
                            message="Halo",
                            request_id=uuid4(),
                        )
                    )
                independent = asyncio.run(
                    self.service().reset(
                        chat_db,
                        raw_session_token=TOKEN_B,
                    )
                )
                self.assertEqual(independent.status, "reset")
            finally:
                lock.release(
                    lock_db,
                    demo_session_id=session_a_id,
                    lease=lease,
                )

    def assert_waiting_reset_revalidates_database_session(
        self,
        *,
        token,
        mode,
    ):
        with self.Session() as seed_db:
            session_id, _owner_id, _reservation_id = self.seed_owner_data(
                seed_db,
                token,
                other=token == TOKEN_B,
            )
        before = self.database_snapshot()

        async def scenario():
            lock_manager = ConversationLockManager(
                wait_timeout_seconds=2,
            )
            service = self.service(lock_manager=lock_manager)
            holder_entered = asyncio.Event()
            holder_release = asyncio.Event()
            key = service._process_lock_key(session_id)

            async def hold_session_lock():
                async with lock_manager.hold(key):
                    holder_entered.set()
                    await holder_release.wait()

            holder = asyncio.create_task(hold_session_lock())
            await asyncio.wait_for(holder_entered.wait(), timeout=2)
            try:
                with self.Session() as reset_db:
                    reset_task = asyncio.create_task(
                        service.reset(
                            reset_db,
                            raw_session_token=token,
                        )
                    )
                    for _attempt in range(100):
                        entry = lock_manager._entries.get(key)
                        if (
                            entry is not None
                            and entry.reference_count == 2
                        ):
                            break
                        await asyncio.sleep(0.01)
                    entry = lock_manager._entries.get(key)
                    self.assertIsNotNone(entry)
                    self.assertEqual(
                        entry.reference_count,
                        2,
                    )
                    self.assertFalse(reset_task.done())

                    with self.Session() as mutation_db:
                        row = self.session_row(mutation_db, token)
                        if mode == "revoke":
                            row.revoked_at = self.now
                        else:
                            row.idle_expires_at = self.now
                        mutation_db.commit()

                    holder_release.set()
                    with self.assertRaises(DemoSessionRequiredError):
                        await asyncio.wait_for(reset_task, timeout=2)
            finally:
                holder_release.set()
                await asyncio.wait_for(holder, timeout=2)

        asyncio.run(scenario())
        after = self.database_snapshot()
        for table_name in (
            "customers",
            "messages",
            "workflows",
            "handoffs",
            "reservations",
            "buckets",
        ):
            self.assertEqual(after[table_name], before[table_name])

        before_session = next(
            row for row in before["sessions"] if row[0] == session_id
        )
        after_session = next(
            row for row in after["sessions"] if row[0] == session_id
        )
        self.assertEqual(after_session[1], before_session[1])
        self.assertEqual(after_session[2], before_session[2])
        self.assertEqual(after_session[3], before_session[3])
        self.assertEqual(after_session[4], before_session[4])
        self.assertEqual(after_session[6], before_session[6])
        if mode == "revoke":
            self.assertEqual(after_session[5], before_session[5])
            self.assertEqual(after_session[7], self.now)
        else:
            self.assertEqual(after_session[5], self.now)
            self.assertEqual(after_session[7], before_session[7])

    def test_revoke_while_reset_waits_is_revalidated_from_postgresql(self):
        self.assert_waiting_reset_revalidates_database_session(
            token=TOKEN_A,
            mode="revoke",
        )

    def test_expiry_while_reset_waits_is_revalidated_from_postgresql(self):
        self.assert_waiting_reset_revalidates_database_session(
            token=TOKEN_B,
            mode="expire",
        )

    def test_invalid_api_bodies_leave_postgresql_state_unchanged(self):
        with self.Session() as seed_db:
            self.seed_owner_data(seed_db, TOKEN_A)
        before = self.database_snapshot()
        with self.api_client(reset_service=self.service()) as client:
            reset_bodies = (
                b"{}",
                b'{"unexpected":"do-not-reflect-marker"}',
                b'["do-not-reflect-marker"]',
                b'"do-not-reflect-marker"',
                b"17",
                b"true",
                b"{",
            )
            for body in reset_bodies:
                with self.subTest(method="POST", body=body):
                    response = client.post(
                        "/internal/demo/reset",
                        headers={
                            **self.headers(TOKEN_A),
                            "Content-Type": "application/json",
                        },
                        content=body,
                    )
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(
                        response.json()["code"],
                        "VALIDATION_ERROR",
                    )
                    self.assertEqual(
                        response.headers["cache-control"],
                        "no-store",
                    )
                    self.assertNotIn(
                        "do-not-reflect-marker",
                        response.text,
                    )

            for body in (
                b"{}",
                b'["do-not-reflect-marker"]',
                b'"do-not-reflect-marker"',
                b"17",
            ):
                with self.subTest(method="GET", body=body):
                    response = client.request(
                        "GET",
                        "/internal/demo/reservations",
                        headers={
                            **self.headers(TOKEN_A),
                            "Content-Type": "application/json",
                        },
                        content=body,
                    )
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn(
                        "do-not-reflect-marker",
                        response.text,
                    )
            query = client.get(
                "/internal/demo/reservations?anything=value",
                headers=self.headers(TOKEN_A),
            )
            self.assertEqual(query.status_code, 422)
        self.assertEqual(self.database_snapshot(), before)

    def test_old_token_remains_valid_through_every_internal_api(self):
        def logical_clock():
            return self.now

        session_service = DemoSessionService(
            token_generator=lambda: TOKEN_C,
            clock=logical_clock,
        )
        core = _ReplyCore()
        chat_service = DemoChatService(
            session_service=session_service,
            lock_manager=ConversationLockManager(),
            database_lock=DemoPostgreSQLAdvisoryLock(),
            core_factory=lambda _session_id: core,
            clock=logical_clock,
        )
        reset_service = self.service(
            session_service=session_service,
            lock_manager=ConversationLockManager(),
            database_lock=DemoPostgreSQLAdvisoryLock(),
        )
        rate_limit_service = DemoRateLimitService(
            session_service=session_service,
            clock=logical_clock,
        )
        with self.api_client(
            session_service=session_service,
            chat_service=chat_service,
            reset_service=reset_service,
            rate_limit_service=rate_limit_service,
        ) as client:
            created = client.post(
                "/internal/demo/sessions",
                headers={
                    "X-BFF-Service-Token": SERVICE_TOKEN,
                    "X-Demo-Client-Subject": CLIENT_SUBJECT,
                },
            )
            self.assertEqual(created.status_code, 201)
            raw_token = created.json()["sessionToken"]
            self.assertEqual(raw_token, TOKEN_C)

            with self.Session() as seed_db:
                _session_id, owner_id, _reservation_id = (
                    self.seed_owner_data(seed_db, raw_token)
                )
                original = self.session_row(seed_db, raw_token)
                original_digest = original.token_digest
                original_absolute = original.absolute_expires_at

            reset = client.post(
                "/internal/demo/reset",
                headers=self.headers(raw_token),
                content=b"",
            )
            self.assertEqual(reset.status_code, 200)
            self.assertEqual(
                reset.headers["cache-control"],
                "no-store",
            )
            second_reset = client.post(
                "/internal/demo/reset",
                headers=self.headers(raw_token),
            )
            self.assertEqual(second_reset.status_code, 200)

            current = client.get(
                "/internal/demo/sessions/current",
                headers=self.headers(raw_token),
            )
            reservations = client.get(
                "/internal/demo/reservations",
                headers=self.headers(raw_token),
            )
            chat = client.post(
                "/internal/demo/chat",
                headers=self.headers(raw_token),
                json={
                    "message": "Halo setelah reset",
                    "requestId": str(uuid4()),
                },
            )
            for response in (current, reservations, chat):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers["cache-control"],
                    "no-store",
                )
                self.assertNotIn("sessionToken", response.text)
                self.assertNotIn(raw_token, response.text)
            self.assertEqual(current.json()["session"]["messageCount"], 0)
            self.assertEqual(reservations.json(), {
                "reservations": [],
                "count": 0,
            })
            self.assertEqual(chat.json()["reply"]["role"], "assistant")
            self.assertEqual(core.calls, 1)

        with self.Session() as verify_db:
            retained = self.session_row(verify_db, TOKEN_C)
            self.assertIsNotNone(retained)
            self.assertEqual(retained.owner_customer_id, owner_id)
            self.assertEqual(retained.token_digest, original_digest)
            self.assertEqual(
                retained.absolute_expires_at,
                original_absolute,
            )
            self.assertIsNotNone(verify_db.get(Customer, owner_id))

    def test_reset_preserves_production_data_and_calls_no_production_paths(self):
        with self.Session() as db:
            self.seed_owner_data(db, TOKEN_A)
            session_b = self.session_row(db, TOKEN_B)
            ticket = SupportTicket(
                ticket_number="AURA-PRESERVE-1",
                owner_customer_id=session_b.owner_customer_id,
                session_reference_hash="c" * 64,
                category="explicit_human_request",
                reason_code="explicit_human_request",
                priority="high",
                safe_summary="Customer requested human assistance.",
                status="open",
                attempt_count=1,
                created_at=self.now,
                updated_at=self.now,
            )
            db.add(ticket)
            db.flush()
            notification = SupportTicketNotification(
                support_ticket_id=ticket.id,
                channel="telegram_owner",
                status="pending",
                attempt_count=0,
                next_attempt_at=self.now,
                created_at=self.now,
                updated_at=self.now,
            )
            identity = TelegramIdentity(
                telegram_user_key="d" * 64,
                customer_id=session_b.owner_customer_id,
                is_active=True,
                created_at=self.now,
                updated_at=self.now,
            )
            db.add_all([notification, identity])
            db.commit()
            ticket_id = ticket.id
            notification_id = notification.id
            identity_id = identity.id

        with (
            patch.object(
                OwnerNotificationDispatcher,
                "process_once",
                autospec=True,
            ) as dispatcher_spy,
            patch.object(
                NotificationOutboxService,
                "enqueue_new_ticket",
                autospec=True,
            ) as outbox_spy,
            patch.object(
                HandoffService,
                "require_handoff",
                autospec=True,
            ) as handoff_spy,
            patch.object(
                ReservationAgent,
                "run",
                autospec=True,
            ) as reservation_agent_spy,
        ):
            with self.Session() as reset_db:
                asyncio.run(
                    self.service().reset(
                        reset_db,
                        raw_session_token=TOKEN_A,
                    )
                )
            dispatcher_spy.assert_not_called()
            outbox_spy.assert_not_called()
            handoff_spy.assert_not_called()
            reservation_agent_spy.assert_not_called()

        with self.Session() as verify_db:
            retained_ticket = verify_db.get(SupportTicket, ticket_id)
            retained_notification = verify_db.get(
                SupportTicketNotification,
                notification_id,
            )
            retained_identity = verify_db.get(
                TelegramIdentity,
                identity_id,
            )
            self.assertIsNotNone(retained_ticket)
            self.assertEqual(retained_ticket.status, "open")
            self.assertEqual(retained_ticket.updated_at, self.now)
            self.assertIsNotNone(retained_notification)
            self.assertEqual(retained_notification.status, "pending")
            self.assertEqual(retained_notification.updated_at, self.now)
            self.assertIsNotNone(retained_identity)
            self.assertTrue(retained_identity.is_active)
            self.assertEqual(retained_identity.updated_at, self.now)
            self.assertEqual(
                verify_db.scalar(
                    select(func.count()).select_from(SupportTicket)
                ),
                1,
            )
            self.assertEqual(
                verify_db.scalar(
                    select(func.count()).select_from(
                        SupportTicketNotification
                    )
                ),
                1,
            )
            self.assertEqual(
                verify_db.scalar(
                    select(func.count()).select_from(TelegramIdentity)
                ),
                1,
            )

    def test_advisory_connection_is_reusable_after_successful_reset(self):
        with self.Session() as db:
            session_id = self.session_row(db, TOKEN_A).id
            asyncio.run(self.service().reset(db, raw_session_token=TOKEN_A))
            lock = DemoPostgreSQLAdvisoryLock()
            lease = lock.acquire(db, demo_session_id=session_id)
            self.assertIsNotNone(lease)
            lock.release(db, demo_session_id=session_id, lease=lease)


if __name__ == "__main__":
    unittest.main()
