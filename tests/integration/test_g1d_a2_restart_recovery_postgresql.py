"""Real PostgreSQL verification for G1D-A2.2 restart recovery."""

import asyncio
import os
from types import SimpleNamespace
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.reservation_agent import ReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_memory import (
    MUTATION_RECONCILIATION_REQUIRED,
    RESERVATION_PERSISTENCE_STATE,
)
from app.core.config import settings
from app.core.conversation_lock_manager import ConversationLockManager
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.memory_errors import ConversationWorkflowPublicationError
from app.db.models.conversation_workflow_state import (
    ConversationWorkflowState,
)
from app.db.models.customer import Customer
from app.db.models.reservation import Reservation
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from app.services.authenticated_chat_service import AuthenticatedChatService
from migrations.add_conversation_workflow_states import (
    INDEXES,
    OWNER_FOREIGN_KEY_NAME,
    OWNER_SESSION_UNIQUE_NAME,
    PAYLOAD_OBJECT_CHECK_NAME,
    REVISION_CHECK_NAME,
    SCHEMA_VERSION_CHECK_NAME,
    SESSION_HASH_CHECK_NAME,
    migrate,
)
from migrations.allow_public_reference_workflow_schema_v2 import (
    migrate as migrate_workflow_v2,
)
from tests.integration.disposable_schema import DisposableSchemaResources


def _identity(url):
    parsed = make_url(url)
    return parsed.get_backend_name(), parsed.host, parsed.port, parsed.database


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return (
            "TEST_DATABASE_URL is not configured; G1D-A2.2 PostgreSQL "
            "tests are skipped."
        )
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if parsed.database != "aura_test":
            return "TEST_DATABASE_URL must target the exact aura_test database."
        if _identity(value) == _identity(settings.DATABASE_URL):
            return (
                "TEST_DATABASE_URL resolves to the normal application "
                "database; refusing to run."
            )
    except Exception:
        return "TEST_DATABASE_URL is invalid; PostgreSQL tests are skipped."
    return None


SKIP_REASON = _skip_reason()


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestG1DA2RestartRecoveryPostgreSQL(unittest.TestCase):
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

        cls.schema = f"aura_g1d_a22_test_{uuid4().hex[:12]}"
        cls.resources = DisposableSchemaResources(
            admin_engine=cls.admin,
            schema=cls.schema,
            allowed_prefixes=("aura_g1d_a22_test_",),
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
        cls.engine = create_engine(schema_url, pool_pre_ping=True)
        cls.resources.track_engine(cls.engine)
        cls.Session = sessionmaker(
            bind=cls.engine,
            autoflush=False,
            autocommit=False,
        )
        Customer.__table__.create(cls.engine)
        Reservation.__table__.create(cls.engine)
        migrate(cls.engine, schema=cls.schema)
        migrate_workflow_v2(cls.engine, schema=cls.schema)

    @classmethod
    def _table(cls, name):
        return f'"{cls.schema}"."{name}"'

    def _owner(self):
        owner = uuid4()
        with self.Session.begin() as db:
            db.add(Customer(id=owner))
        return owner

    @staticmethod
    def _seed_create(memory, key):
        memory.get_session(key).update(
            {
                "intent": "reservation",
                "name": "Rizal",
                "people": 4,
                "date": "2026-08-01",
                "time": "19:00",
                "completed": False,
                "awaiting_confirmation": True,
                "editing_field": None,
                "asked_fields": ["name", "people", "date", "time"],
            }
        )

    def _seed_reservation(self, owner, *, people=4):
        with self.Session.begin() as db:
            reservation = Reservation(
                owner_customer_id=owner,
                name="Rizal",
                people=people,
                date="2026-08-01",
                time="19:00",
                status="pending",
                public_reference="RSV_42424242424242424242424242424242",
            )
            db.add(reservation)
            db.flush()
            return reservation.id, reservation.public_reference

    def test_01_migration_is_additive_idempotent_and_repairs_safe_objects(self):
        with self.engine.begin() as connection:
            connection.execute(text(
                f"INSERT INTO {self._table('reservations')} "
                "(name, people, date, time, status) "
                "VALUES ('Migration Marker', 2, '2026-08-01', '18:00', "
                "'pending')"
            ))
        with self.engine.connect() as connection:
            before = connection.execute(text(
                f"SELECT id, name, people, date, time, status "
                f"FROM {self._table('reservations')} ORDER BY id"
            )).all()

        self.assertFalse(migrate(self.engine, schema=self.schema))
        with self.engine.begin() as connection:
            connection.execute(text(
                f"DROP INDEX {self._table(next(iter(INDEXES)))}"
            ))
            connection.execute(text(
                f"ALTER TABLE {self._table('conversation_workflow_states')} "
                f'DROP CONSTRAINT "{REVISION_CHECK_NAME}"'
            ))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        self.assertFalse(migrate(self.engine, schema=self.schema))

        with self.engine.connect() as connection:
            after = connection.execute(text(
                f"SELECT id, name, people, date, time, status "
                f"FROM {self._table('reservations')} ORDER BY id"
            )).all()
        self.assertEqual(before, after)

    def test_02_schema_constraints_and_indexes_are_declared(self):
        inspector = inspect(self.engine)
        columns = {
            item["name"]: item
            for item in inspector.get_columns(
                "conversation_workflow_states",
                schema=self.schema,
            )
        }
        self.assertEqual(
            set(columns),
            {
                "id",
                "owner_customer_id",
                "session_reference_hash",
                "schema_version",
                "payload",
                "is_active",
                "revision",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(
            inspector.get_pk_constraint(
                "conversation_workflow_states",
                schema=self.schema,
            )["name"],
            "pk_conversation_workflow_states",
        )
        foreign_keys = inspector.get_foreign_keys(
            "conversation_workflow_states",
            schema=self.schema,
        )
        self.assertIn(
            OWNER_FOREIGN_KEY_NAME,
            {item["name"] for item in foreign_keys},
        )
        unique_names = {
            item["name"]
            for item in inspector.get_unique_constraints(
                "conversation_workflow_states",
                schema=self.schema,
            )
        }
        self.assertIn(OWNER_SESSION_UNIQUE_NAME, unique_names)
        check_names = {
            item["name"]
            for item in inspector.get_check_constraints(
                "conversation_workflow_states",
                schema=self.schema,
            )
        }
        self.assertTrue(
            {
                SCHEMA_VERSION_CHECK_NAME,
                REVISION_CHECK_NAME,
                SESSION_HASH_CHECK_NAME,
                PAYLOAD_OBJECT_CHECK_NAME,
            }.issubset(check_names)
        )
        index_names = {
            item["name"]
            for item in inspector.get_indexes(
                "conversation_workflow_states",
                schema=self.schema,
            )
        }
        self.assertTrue(set(INDEXES).issubset(index_names))

    def test_03_incompatible_existing_table_fails_closed(self):
        incompatible_schema = f"aura_g1d_a22_test_{uuid4().hex[:12]}"
        self.resources.track_schema(incompatible_schema)
        with self.admin.begin() as connection:
            connection.execute(text(
                f'CREATE SCHEMA "{incompatible_schema}"'
            ))
            connection.execute(text(f"""
                CREATE TABLE "{incompatible_schema}".customers (
                    id UUID PRIMARY KEY
                )
            """))
            connection.execute(text(f"""
                CREATE TABLE "{incompatible_schema}".
                    conversation_workflow_states (
                        id SERIAL PRIMARY KEY,
                        owner_customer_id UUID NOT NULL,
                        session_reference_hash VARCHAR(64) NOT NULL,
                        schema_version INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        is_active BOOLEAN NOT NULL,
                        revision INTEGER NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
            """))
        with self.assertRaises(RuntimeError):
            migrate(self.admin, schema=incompatible_schema)

    def test_04_customer_session_round_trip_and_stale_writer_protection(self):
        owner = self._owner()
        key = build_authenticated_memory_key(owner, "round-trip")
        initial_memory = MemoryManager()
        self._seed_create(initial_memory, key)
        initial = ConversationWorkflowStateService(initial_memory)
        with self.Session() as db:
            initial.publish(
                db,
                owner_customer_id=owner,
                memory_key=key,
            )
        with self.Session() as db:
            row = db.scalar(
                select(ConversationWorkflowState).where(
                    ConversationWorkflowState.owner_customer_id == owner,
                    ConversationWorkflowState.session_reference_hash
                    == initial.hash_session_reference(key),
                )
            )
            self.assertEqual(row.schema_version, 2)
            self.assertNotIn("reservation_id", row.payload)
            self.assertNotIn("cancel_reservation_id", row.payload)

        winner_memory = MemoryManager()
        stale_memory = MemoryManager()
        winner = ConversationWorkflowStateService(winner_memory)
        stale = ConversationWorkflowStateService(stale_memory)
        for service in (winner, stale):
            with self.Session() as db:
                service.restore(
                    db,
                    owner_customer_id=owner,
                    memory_key=key,
                )
        self.assertTrue(
            winner_memory.get_session(key)["awaiting_confirmation"]
        )

        winner_memory.get_session(key)["completed"] = True
        winner_memory.get_session(key)["awaiting_confirmation"] = False
        with self.Session() as db:
            winner.publish(
                db,
                owner_customer_id=owner,
                memory_key=key,
            )
        with self.Session() as db:
            with self.assertRaises(ConversationWorkflowPublicationError):
                stale.publish(
                    db,
                    owner_customer_id=owner,
                    memory_key=key,
                )

        with self.Session() as db:
            row = db.scalar(select(ConversationWorkflowState).where(
                ConversationWorkflowState.owner_customer_id == owner
            ))
            self.assertFalse(row.is_active)
            self.assertEqual(row.payload, {})
            serialized = str(row.payload)
            self.assertNotIn(str(owner), serialized)
            self.assertNotIn("round-trip", serialized)

    def test_05_create_commit_crash_marker_blocks_duplicate_after_restart(self):
        owner = self._owner()
        key = build_authenticated_memory_key(owner, "create-crash")
        memory = MemoryManager()
        self._seed_create(memory, key)
        persistence = ConversationWorkflowStateService(memory)
        with self.Session() as db:
            persistence.publish(
                db,
                owner_customer_id=owner,
                memory_key=key,
            )
            agent = ReservationAgent(
                memory_manager=memory,
                workflow_state_service=persistence,
            )
            result = asyncio.run(agent.handle_confirmation(
                "Ya",
                key,
                owner_customer_id=owner,
                db=db,
            ))
        self.assertEqual(result["status"], "completed")

        restarted_memory = MemoryManager()
        restarted = ConversationWorkflowStateService(restarted_memory)
        with self.Session() as db:
            restarted.restore(
                db,
                owner_customer_id=owner,
                memory_key=key,
            )
            blocker = restarted_memory.get_session(key)[
                RESERVATION_PERSISTENCE_STATE
            ]
            self.assertEqual(
                blocker["status"],
                MUTATION_RECONCILIATION_REQUIRED,
            )
            retry = asyncio.run(
                ReservationAgent(
                    memory_manager=restarted_memory,
                ).handle_confirmation(
                    "Ya",
                    key,
                    owner_customer_id=owner,
                    db=db,
                )
            )
        self.assertEqual(retry["status"], "persistence_uncertain")
        with self.Session() as db:
            count = db.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.owner_customer_id == owner)
            )
        self.assertEqual(count, 1)

    def test_06_update_and_cancel_commit_markers_survive_restart(self):
        owner = self._owner()
        reservation_id, reservation_reference = self._seed_reservation(owner)
        cases = (
            (
                "update",
                {
                    "update_reservation_stage": "input_value",
                    "reservation_reference": reservation_reference,
                    "editing_field": "people",
                },
                "6",
            ),
            (
                "cancel",
                {
                    "cancel_reservation_stage": "confirm_cancellation",
                    "cancel_reservation_reference": reservation_reference,
                },
                "Ya",
            ),
        )
        for operation, state, message in cases:
            with self.subTest(operation=operation):
                key = build_authenticated_memory_key(
                    owner,
                    f"{operation}-crash",
                )
                memory = MemoryManager()
                memory.replace_reservation_workflow_state(key, state)
                persistence = ConversationWorkflowStateService(memory)
                with self.Session() as db:
                    persistence.publish(
                        db,
                        owner_customer_id=owner,
                        memory_key=key,
                    )
                with self.Session() as db:
                    persisted = db.scalar(
                        select(ConversationWorkflowState).where(
                            ConversationWorkflowState.owner_customer_id
                            == owner,
                            ConversationWorkflowState.session_reference_hash
                            == persistence.hash_session_reference(key),
                        )
                    )
                    self.assertEqual(persisted.schema_version, 2)
                    self.assertNotIn("reservation_id", persisted.payload)
                    self.assertNotIn(
                        "cancel_reservation_id",
                        persisted.payload,
                    )
                if operation == "update":
                    agent = UpdateReservationAgent(
                        memory,
                        workflow_state_service=persistence,
                    )
                else:
                    agent = CancelReservationAgent(
                        memory,
                        workflow_state_service=persistence,
                    )
                with self.Session() as db:
                    result = asyncio.run(
                        agent.run(db, key, message, owner)
                    )
                self.assertIn(
                    result["status"],
                    {"updated", "cancelled"},
                )

                restarted_memory = MemoryManager()
                with self.Session() as db:
                    ConversationWorkflowStateService(
                        restarted_memory
                    ).restore(
                        db,
                        owner_customer_id=owner,
                        memory_key=key,
                    )
                blocker = restarted_memory.get_session(key)[
                    RESERVATION_PERSISTENCE_STATE
                ]
                self.assertEqual(blocker["operation"], operation)
                self.assertEqual(
                    blocker["status"],
                    MUTATION_RECONCILIATION_REQUIRED,
                )

    def test_07_recovery_transaction_ends_before_agent_await(self):
        owner = self._owner()
        memory = MemoryManager()
        persistence = ConversationWorkflowStateService(memory)

        class Handoff:
            def restore_active_handoff(self, *_args):
                return None

            @staticmethod
            def recovery_error_response():
                return "safe"

        class Agent:
            handoff_service = Handoff()

            async def handle(agent_self, **kwargs):
                self.assertFalse(kwargs["db"].in_transaction())
                await asyncio.sleep(0)
                self.assertFalse(kwargs["db"].in_transaction())
                memory.get_session(kwargs["session_id"]).update(
                    {
                        "intent": "reservation",
                        "name": None,
                        "people": None,
                        "date": None,
                        "time": None,
                        "completed": False,
                        "awaiting_confirmation": False,
                        "editing_field": None,
                        "asked_fields": ["name"],
                    }
                )
                return "ok"

        service = AuthenticatedChatService(
            agent=Agent(),
            lock_manager=ConversationLockManager(),
            workflow_state_service=persistence,
        )
        with self.Session() as db:
            response = asyncio.run(service.process(
                db=db,
                customer=SimpleNamespace(id=owner),
                session_reference="transaction-boundary",
                message="buat reservasi",
            ))
            self.assertFalse(db.in_transaction())
        self.assertEqual(response, "ok")


if __name__ == "__main__":
    unittest.main()
