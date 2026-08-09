"""Guarded PostgreSQL coverage for workflow snapshot v2 compatibility."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
from time import monotonic
from unittest.mock import patch
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.brain.memory_manager import MemoryManager
from app.core.config import settings
from app.core.transaction_errors import PersistenceOperationError
from app.db.models.conversation_workflow_state import (
    ConversationWorkflowState,
)
from app.db.models.customer import Customer
from app.db.models.reservation import Reservation
from app.db.repositories.conversation_workflow_state_repository import (
    ConversationWorkflowStateRepository,
)
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
    WorkflowV1ConversionOutcome,
)
from app.services.reservation.service import ReservationService
from migrations.add_conversation_workflow_states import (
    migrate as migrate_workflow_v1,
)
from migrations.allow_public_reference_workflow_schema_v2 import (
    CONSTRAINT,
    WorkflowSchemaV2MigrationError,
    _constraint_version,
    downgrade,
    migrate,
)
from tests.integration.disposable_schema import DisposableSchemaResources


REFERENCE = "RSV_" + ("b" * 32)


def _identity(url):
    parsed = make_url(url)
    return parsed.get_backend_name(), parsed.host, parsed.port, parsed.database


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return "Guarded PostgreSQL test configuration is unavailable."
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "Guarded database is not PostgreSQL."
        if parsed.database != "aura_test":
            return "Guarded database name is not the dedicated test database."
        if _identity(value) == _identity(settings.DATABASE_URL):
            return "Guarded database resolves to the application database."
    except Exception:
        return "Guarded PostgreSQL test configuration is invalid."
    return None


SKIP_REASON = _skip_reason()


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class WorkflowSnapshotV2PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(
            os.environ["TEST_DATABASE_URL"],
            poolclass=NullPool,
        )
        with cls.admin.connect() as connection:
            identity = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
        if identity != ("aura_test", "aura_test_runner"):
            cls.admin.dispose()
            raise RuntimeError("Dedicated PostgreSQL identity did not match.")

        cls.schema = f"aura_workflow_v2_test_{uuid4().hex[:12]}"
        cls.resources = DisposableSchemaResources(
            admin_engine=cls.admin,
            schema=cls.schema,
            allowed_prefixes=("aura_workflow_v2_test_",),
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
        cls.engine = create_engine(schema_url, poolclass=NullPool)
        cls.resources.track_engine(cls.engine)
        cls.Session = sessionmaker(
            bind=cls.engine,
            autoflush=False,
            autocommit=False,
        )
        Customer.__table__.create(cls.engine)
        Reservation.__table__.create(cls.engine)
        migrate_workflow_v1(cls.engine, schema=cls.schema)
        cls.initial_migration_changed = migrate(
            cls.engine,
            schema=cls.schema,
        )

    @classmethod
    def _table(cls, name, *, schema=None):
        return f'"{schema or cls.schema}"."{name}"'

    @classmethod
    def _new_schema(cls):
        schema = f"aura_workflow_v2_test_{uuid4().hex[:12]}"
        cls.resources.track_schema(schema)
        with cls.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_url = make_url(
            os.environ["TEST_DATABASE_URL"]
        ).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(schema_url, poolclass=NullPool)
        cls.resources.track_engine(engine)
        Customer.__table__.create(engine)
        Reservation.__table__.create(engine)
        migrate_workflow_v1(engine, schema=schema)
        return schema, engine

    @staticmethod
    def _check(engine, schema):
        constraints = inspect(engine).get_check_constraints(
            "conversation_workflow_states",
            schema=schema,
        )
        return next(item for item in constraints if item["name"] == CONSTRAINT)

    @staticmethod
    def _column(engine, schema, name):
        columns = inspect(engine).get_columns(
            "conversation_workflow_states",
            schema=schema,
        )
        return next(item for item in columns if item["name"] == name)

    def _owner(self):
        owner = uuid4()
        with self.Session.begin() as db:
            db.add(Customer(id=owner))
        return owner

    def _reservation(self, owner, *, reference=REFERENCE):
        with self.Session.begin() as db:
            row = Reservation(
                owner_customer_id=owner,
                public_reference=reference,
                name="Test Owner",
                people=4,
                date="2026-08-01",
                time="19:00",
                status="pending",
            )
            db.add(row)
            db.flush()
            identifier = row.id
        return identifier

    def _workflow(self, owner, payload, *, version=1, revision=1):
        key = f"workflow-{uuid4().hex}"
        session_hash = ConversationWorkflowStateService.hash_session_reference(
            key
        )
        with self.Session.begin() as db:
            row = ConversationWorkflowState(
                owner_customer_id=owner,
                session_reference_hash=session_hash,
                schema_version=version,
                payload=payload,
                is_active=True,
                revision=revision,
            )
            db.add(row)
        return key, session_hash

    def test_01_migration_converges_from_v1_without_changing_rows(self):
        schema, engine = self._new_schema()
        owner = uuid4()
        payload = {
            "update_reservation_stage": "select_reservation_id",
            "reservation_id": None,
            "editing_field": None,
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {self._table('customers', schema=schema)} "
                    "(id, token_version, is_active, created_at) "
                    "VALUES (:owner, 1, TRUE, CURRENT_TIMESTAMP)"
                ),
                {"owner": owner},
            )
            connection.execute(
                text(
                    f"INSERT INTO {self._table('conversation_workflow_states', schema=schema)} "
                    "(owner_customer_id, session_reference_hash, "
                    "schema_version, payload, is_active, revision) "
                    "VALUES (:owner, :scope, 1, CAST(:payload AS JSONB), "
                    "TRUE, 8)"
                ),
                {
                    "owner": owner,
                    "scope": "c" * 64,
                    "payload": (
                        '{"update_reservation_stage":"select_reservation_id",'
                        '"reservation_id":null,"editing_field":null}'
                    ),
                },
            )
        with engine.connect() as connection:
            before = connection.execute(text(
                f"SELECT schema_version, payload, is_active, revision "
                f"FROM {self._table('conversation_workflow_states', schema=schema)}"
            )).one()
        self.assertTrue(migrate(engine, schema=schema))
        self.assertFalse(migrate(engine, schema=schema))
        with engine.connect() as connection:
            after = connection.execute(text(
                f"SELECT schema_version, payload, is_active, revision "
                f"FROM {self._table('conversation_workflow_states', schema=schema)}"
            )).one()
        self.assertEqual(before, after)
        self.assertEqual(_constraint_version(self._check(engine, schema)["sqltext"]), 2)

    def test_02_missing_constraint_is_added_but_weak_constraints_fail_closed(self):
        schema, engine = self._new_schema()
        with engine.begin() as connection:
            connection.execute(text(
                f"ALTER TABLE {self._table('conversation_workflow_states', schema=schema)} "
                f'DROP CONSTRAINT "{CONSTRAINT}"'
            ))
        self.assertTrue(migrate(engine, schema=schema))

        expressions = (
            "schema_version >= 1",
            "schema_version BETWEEN 1 AND 2",
            "schema_version IN (1, 2, 3)",
            "schema_version > 0 AND schema_version < 3",
            "schema_version = 1 OR schema_version = 2 OR is_active",
            "1 = 1",
            "revision IN (1, 2)",
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                candidate_schema, candidate_engine = self._new_schema()
                with candidate_engine.begin() as connection:
                    connection.execute(text(
                        f"ALTER TABLE {self._table('conversation_workflow_states', schema=candidate_schema)} "
                        f'DROP CONSTRAINT "{CONSTRAINT}"'
                    ))
                    connection.execute(text(
                        f"ALTER TABLE {self._table('conversation_workflow_states', schema=candidate_schema)} "
                        f'ADD CONSTRAINT "{CONSTRAINT}" CHECK ({expression})'
                    ))
                with self.assertRaises(WorkflowSchemaV2MigrationError):
                    migrate(candidate_engine, schema=candidate_schema)

        exact_schema, exact_engine = self._new_schema()
        with exact_engine.begin() as connection:
            connection.execute(text(
                f"ALTER TABLE {self._table('conversation_workflow_states', schema=exact_schema)} "
                f'DROP CONSTRAINT "{CONSTRAINT}"'
            ))
            connection.execute(text(
                f"ALTER TABLE {self._table('conversation_workflow_states', schema=exact_schema)} "
                f'ADD CONSTRAINT "{CONSTRAINT}" '
                'CHECK ((("schema_version" IN (1, 2))))'
            ))
        self.assertFalse(migrate(exact_engine, schema=exact_schema))

    def test_03_unnamed_or_malformed_constraint_fails_closed(self):
        for named in (False, True):
            with self.subTest(named=named):
                schema, engine = self._new_schema()
                with engine.begin() as connection:
                    connection.execute(text(
                        f"ALTER TABLE {self._table('conversation_workflow_states', schema=schema)} "
                        f'DROP CONSTRAINT "{CONSTRAINT}"'
                    ))
                    name = f'CONSTRAINT "{CONSTRAINT}" ' if named else ""
                    connection.execute(text(
                        f"ALTER TABLE {self._table('conversation_workflow_states', schema=schema)} "
                        f"ADD {name}CHECK (schema_version = 1 OR schema_version = 2)"
                    ))
                with self.assertRaises(WorkflowSchemaV2MigrationError):
                    migrate(engine, schema=schema)

        schema, engine = self._new_schema()
        with engine.begin() as connection:
            connection.execute(text(
                f"ALTER TABLE {self._table('conversation_workflow_states', schema=schema)} "
                "ADD CHECK (schema_version >= 1)"
            ))
        with self.assertRaises(WorkflowSchemaV2MigrationError):
            migrate(engine, schema=schema)

    def test_03a_schema_version_column_metadata_is_exact_and_revalidated(self):
        schema, engine = self._new_schema()
        self.assertTrue(migrate(engine, schema=schema))
        column = self._column(engine, schema, "schema_version")
        self.assertEqual(column["type"].__class__.__name__.upper(), "INTEGER")
        self.assertEqual(str(column["type"]).upper(), "INTEGER")
        self.assertFalse(column["nullable"])
        self.assertIn(str(column.get("default") or "").lower(), {"1", "1::integer"})
        self.assertIsNone(column.get("identity"))
        self.assertIsNone(column.get("computed"))

    def test_03b_schema_version_column_metadata_mismatch_fails_closed(self):
        mutations = (
            "ALTER COLUMN schema_version DROP NOT NULL",
            "ALTER COLUMN schema_version TYPE BIGINT",
            "ALTER COLUMN schema_version SET DEFAULT 2",
            "DROP COLUMN schema_version CASCADE",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation.split()[0:3]):
                schema, engine = self._new_schema()
                with engine.begin() as connection:
                    connection.execute(text(
                        f"ALTER TABLE {self._table('conversation_workflow_states', schema=schema)} "
                        f"{mutation}"
                    ))
                with self.assertRaises(WorkflowSchemaV2MigrationError):
                    migrate(engine, schema=schema)

    def test_03c_initial_migration_is_forward_convergent_after_v2(self):
        schema, engine = self._new_schema()
        owner = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {self._table('customers', schema=schema)} "
                    "(id, token_version, is_active, created_at) "
                    "VALUES (:owner, 1, TRUE, CURRENT_TIMESTAMP)"
                ),
                {"owner": owner},
            )
        self.assertTrue(migrate(engine, schema=schema))
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {self._table('conversation_workflow_states', schema=schema)} "
                    "(owner_customer_id, session_reference_hash, "
                    "schema_version, payload, is_active, revision) "
                    "VALUES (:owner, :scope, 2, '{}'::JSONB, FALSE, 7)"
                ),
                {"owner": owner, "scope": "e" * 64},
            )
        with engine.connect() as connection:
            before = connection.execute(text(
                f"SELECT schema_version, payload, is_active, revision "
                f"FROM {self._table('conversation_workflow_states', schema=schema)}"
            )).one()
        self.assertFalse(migrate_workflow_v1(engine, schema=schema))
        with engine.connect() as connection:
            after = connection.execute(text(
                f"SELECT schema_version, payload, is_active, revision "
                f"FROM {self._table('conversation_workflow_states', schema=schema)}"
            )).one()
        self.assertEqual(before, after)
        self.assertEqual(_constraint_version(self._check(engine, schema)["sqltext"]), 2)

    def test_03d_initial_migration_rejects_weak_newer_constraint(self):
        for expression in (
            "schema_version >= 1",
            "schema_version IN (1, 2, 3)",
        ):
            with self.subTest(expression=expression):
                schema, engine = self._new_schema()
                with engine.begin() as connection:
                    connection.execute(text(
                        f"ALTER TABLE {self._table('conversation_workflow_states', schema=schema)} "
                        f'DROP CONSTRAINT "{CONSTRAINT}"'
                    ))
                    connection.execute(text(
                        f"ALTER TABLE {self._table('conversation_workflow_states', schema=schema)} "
                        f'ADD CONSTRAINT "{CONSTRAINT}" CHECK ({expression})'
                    ))
                with self.assertRaises(RuntimeError):
                    migrate_workflow_v1(engine, schema=schema)

        schema, engine = self._new_schema()
        with engine.begin() as connection:
            connection.execute(text(
                f"ALTER TABLE {self._table('conversation_workflow_states', schema=schema)} "
                'ADD CONSTRAINT "ck_workflow_schema_competing" '
                "CHECK (schema_version >= 1)"
            ))
        with self.assertRaises(RuntimeError):
            migrate_workflow_v1(engine, schema=schema)

    def test_04_failed_ddl_rolls_back_the_original_v1_constraint(self):
        schema, engine = self._new_schema()
        from migrations import allow_public_reference_workflow_schema_v2 as module

        original_text = module.text

        def broken_text(statement):
            if "ADD CONSTRAINT" in statement:
                return original_text("INVALID WORKFLOW DDL")
            return original_text(statement)

        with patch.object(module, "text", side_effect=broken_text):
            with self.assertRaises(WorkflowSchemaV2MigrationError):
                migrate(engine, schema=schema)
        self.assertEqual(_constraint_version(self._check(engine, schema)["sqltext"]), 1)

    def test_05_downgrade_requires_zero_v2_rows(self):
        schema, engine = self._new_schema()
        migrate(engine, schema=schema)
        self.assertTrue(downgrade(engine, schema=schema))
        self.assertFalse(downgrade(engine, schema=schema))
        migrate(engine, schema=schema)

        owner = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {self._table('customers', schema=schema)} "
                    "(id, token_version, is_active, created_at) "
                    "VALUES (:owner, 1, TRUE, CURRENT_TIMESTAMP)"
                ),
                {"owner": owner},
            )
            connection.execute(
                text(
                    f"INSERT INTO {self._table('conversation_workflow_states', schema=schema)} "
                    "(owner_customer_id, session_reference_hash, "
                    "schema_version, payload, is_active, revision) "
                    "VALUES (:owner, :scope, 2, '{}'::JSONB, FALSE, 1)"
                ),
                {"owner": owner, "scope": "d" * 64},
            )
        with self.assertRaises(WorkflowSchemaV2MigrationError):
            downgrade(engine, schema=schema)
        self.assertEqual(_constraint_version(self._check(engine, schema)["sqltext"]), 2)

    def test_06_constraint_accepts_only_versions_one_and_two(self):
        self.assertTrue(self.initial_migration_changed)
        owner = self._owner()
        table = self._table("conversation_workflow_states")
        for version in (1, 2):
            with self.subTest(version=version), self.engine.begin() as connection:
                connection.execute(
                    text(
                        f"INSERT INTO {table} "
                        "(owner_customer_id, session_reference_hash, "
                        "schema_version, payload, is_active, revision) "
                        "VALUES (:owner, :scope, :version, '{}'::JSONB, "
                        "FALSE, 1)"
                    ),
                    {
                        "owner": owner,
                        "scope": str(version) * 64,
                        "version": version,
                    },
                )
        for version in (0, 3, -1, None, "invalid"):
            with self.subTest(version=version):
                with self.assertRaises((IntegrityError, DBAPIError, TypeError)):
                    with self.engine.begin() as connection:
                        connection.execute(
                            text(
                                f"INSERT INTO {table} "
                                "(owner_customer_id, session_reference_hash, "
                                "schema_version, payload, is_active, revision) "
                                "VALUES (:owner, :scope, :version, '{}'::JSONB, "
                                "FALSE, 1)"
                            ),
                            {
                                "owner": owner,
                                "scope": uuid4().hex * 2,
                                "version": version,
                            },
                        )

    def test_07_real_conversion_is_owner_scoped_atomic_and_revision_safe(self):
        owner = self._owner()
        concurrency_reference = "RSV_" + uuid4().hex
        reservation_id = self._reservation(
            owner,
            reference=concurrency_reference,
        )
        key, session_hash = self._workflow(
            owner,
            {
                "update_reservation_stage": "input_value",
                "reservation_id": reservation_id,
                "editing_field": "people",
            },
        )
        service = ConversationWorkflowStateService(MemoryManager())
        with self.Session() as db:
            outcome = service.convert_v1_state_to_v2(
                db,
                owner_customer_id=owner,
                memory_key=key,
                expected_revision=1,
            )
        self.assertEqual(outcome, WorkflowV1ConversionOutcome.CONVERTED)
        with self.Session() as db:
            row = db.scalar(select(ConversationWorkflowState).where(
                ConversationWorkflowState.session_reference_hash
                == session_hash
            ))
            self.assertEqual(row.schema_version, 2)
            self.assertEqual(row.revision, 2)
            self.assertTrue(row.is_active)
            self.assertEqual(
                row.payload["reservation_reference"],
                concurrency_reference,
            )
            self.assertNotIn("reservation_id", row.payload)

        with self.Session() as db:
            outcome = service.convert_v1_state_to_v2(
                db,
                owner_customer_id=owner,
                memory_key=key,
                expected_revision=1,
            )
        self.assertEqual(
            outcome,
            WorkflowV1ConversionOutcome.REVISION_CONFLICT,
        )

    def test_08_cross_owner_missing_and_null_reference_become_same_tombstone(self):
        owner = self._owner()
        other_owner = self._owner()
        foreign_id = self._reservation(
            other_owner,
            reference="RSV_" + ("c" * 32),
        )
        null_id = self._reservation(owner, reference=None)
        cases = (foreign_id, null_id, max(foreign_id, null_id) + 1000)
        for reservation_id in cases:
            with self.subTest(case=len(str(reservation_id))):
                key, session_hash = self._workflow(
                    owner,
                    {
                        "cancel_reservation_stage": "confirm_cancellation",
                        "cancel_reservation_id": reservation_id,
                    },
                )
                service = ConversationWorkflowStateService(MemoryManager())
                with self.Session() as db:
                    outcome = service.convert_v1_state_to_v2(
                        db,
                        owner_customer_id=owner,
                        memory_key=key,
                        expected_revision=1,
                    )
                self.assertEqual(
                    outcome,
                    WorkflowV1ConversionOutcome.UNAVAILABLE,
                )
                with self.Session() as db:
                    row = db.scalar(select(ConversationWorkflowState).where(
                        ConversationWorkflowState.session_reference_hash
                        == session_hash
                    ))
                    self.assertEqual(row.schema_version, 2)
                    self.assertEqual(row.payload, {})
                    self.assertFalse(row.is_active)
                    self.assertEqual(row.revision, 2)

    def test_09_failure_after_replace_rolls_back_version_payload_and_revision(self):
        owner = self._owner()
        key, session_hash = self._workflow(
            owner,
            {
                "update_reservation_stage": "select_reservation_id",
                "reservation_id": None,
                "editing_field": None,
            },
        )

        class FailingRepository(ConversationWorkflowStateRepository):
            @staticmethod
            def replace(row, *, schema_version, payload, is_active):
                ConversationWorkflowStateRepository.replace(
                    row,
                    schema_version=schema_version,
                    payload=payload,
                    is_active=is_active,
                )
                raise RuntimeError("forced rollback")

        service = ConversationWorkflowStateService(
            MemoryManager(),
            repository=FailingRepository(),
        )
        with self.Session() as db:
            with self.assertRaises(PersistenceOperationError):
                service.convert_v1_state_to_v2(
                    db,
                    owner_customer_id=owner,
                    memory_key=key,
                    expected_revision=1,
                )
        with self.Session() as db:
            row = db.scalar(select(ConversationWorkflowState).where(
                ConversationWorkflowState.session_reference_hash
                == session_hash
            ))
            self.assertEqual(row.schema_version, 1)
            self.assertEqual(row.revision, 1)
            self.assertIn("reservation_id", row.payload)

    def test_10_two_transactions_preserve_newer_state_and_block_tombstone(self):
        owner = self._owner()
        reservation_id = self._reservation(owner)
        key, session_hash = self._workflow(
            owner,
            {
                "update_reservation_stage": "select_field",
                "reservation_id": reservation_id,
                "editing_field": None,
            },
        )
        holder_locked = threading.Event()
        release_holder = threading.Event()
        waiter_started = threading.Event()
        pids = {}

        class HoldingRepository(ConversationWorkflowStateRepository):
            def get_by_scope(inner_self, db, **kwargs):
                row = super().get_by_scope(db, **kwargs)
                pids["holder"] = db.scalar(text("SELECT pg_backend_pid()"))
                holder_locked.set()
                if not release_holder.wait(timeout=10):
                    raise RuntimeError("bounded workflow lock test timed out")
                return row

        class WaitingRepository(ConversationWorkflowStateRepository):
            def __init__(inner_self):
                inner_self.replace_calls = 0

            def get_by_scope(inner_self, db, **kwargs):
                pids["waiter"] = db.scalar(text("SELECT pg_backend_pid()"))
                waiter_started.set()
                return super().get_by_scope(db, **kwargs)

            def replace(inner_self, row, **kwargs):
                inner_self.replace_calls += 1
                return ConversationWorkflowStateRepository.replace(row, **kwargs)

        class TombstoneCandidateReservationRepository:
            def __init__(inner_self):
                inner_self.calls = 0

            def get_by_id_for_workflow_v1_conversion(
                inner_self,
                _db,
                _reservation_id,
                _owner_customer_id,
            ):
                inner_self.calls += 1
                return None

        waiting_repository = WaitingRepository()
        tombstone_candidate = TombstoneCandidateReservationRepository()
        holder_service = ConversationWorkflowStateService(
            MemoryManager(),
            repository=HoldingRepository(),
        )
        waiter_service = ConversationWorkflowStateService(
            MemoryManager(),
            repository=waiting_repository,
            reservation_repository=tombstone_candidate,
        )

        def convert(service):
            with self.Session() as db:
                return service.convert_v1_state_to_v2(
                    db,
                    owner_customer_id=owner,
                    memory_key=key,
                    expected_revision=1,
                )

        blocked = False
        poll_pause = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as executor:
            holder_future = executor.submit(convert, holder_service)
            self.assertTrue(holder_locked.wait(timeout=10))
            waiter_future = executor.submit(convert, waiter_service)
            self.assertTrue(waiter_started.wait(timeout=10))
            try:
                deadline = monotonic() + 10
                while monotonic() < deadline:
                    with self.admin.connect() as connection:
                        blockers = connection.scalar(
                            text("SELECT pg_blocking_pids(:waiter)"),
                            {"waiter": pids["waiter"]},
                        ) or []
                    if pids["holder"] in blockers:
                        blocked = True
                        break
                    poll_pause.wait(timeout=0.01)
            finally:
                release_holder.set()
            holder_outcome = holder_future.result(timeout=10)
            waiter_outcome = waiter_future.result(timeout=10)

        self.assertTrue(blocked)
        self.assertEqual(holder_outcome, WorkflowV1ConversionOutcome.CONVERTED)
        self.assertEqual(
            waiter_outcome,
            WorkflowV1ConversionOutcome.REVISION_CONFLICT,
        )
        self.assertEqual(waiting_repository.replace_calls, 0)
        self.assertEqual(tombstone_candidate.calls, 0)
        with self.Session() as db:
            row = db.scalar(select(ConversationWorkflowState).where(
                ConversationWorkflowState.owner_customer_id == owner,
                ConversationWorkflowState.session_reference_hash
                == session_hash,
            ))
            self.assertEqual(row.schema_version, 2)
            self.assertEqual(row.revision, 2)
            self.assertTrue(row.is_active)
            self.assertEqual(row.payload["reservation_reference"], REFERENCE)
            self.assertNotIn("reservation_id", row.payload)
            self.assertEqual(db.scalar(text("SELECT 1")), 1)

    def test_11_candidate_order_survives_publish_and_restart_restore(self):
        owner = self._owner()
        memory_key = f"candidate-order-{uuid4().hex}"
        candidate_references = [
            "RSV_" + ("2" * 32),
            "RSV_" + ("1" * 32),
        ]
        page_cursor = "RSV_" + ("3" * 32)
        first_memory = MemoryManager()
        first_memory.get_session(memory_key).update(
            {
                "update_reservation_stage": "select_reservation_reference",
                "update_reservation_candidate_references": candidate_references,
                "update_reservation_page_cursor": page_cursor,
                "update_reservation_page_has_more": False,
                "reservation_reference": None,
                "editing_field": None,
            }
        )
        with self.Session() as db:
            ConversationWorkflowStateService(first_memory).publish(
                db,
                owner_customer_id=owner,
                memory_key=memory_key,
            )

        restarted_memory = MemoryManager()
        with self.Session() as db:
            ConversationWorkflowStateService(restarted_memory).restore(
                db,
                owner_customer_id=owner,
                memory_key=memory_key,
            )
        restored = restarted_memory.get_session(memory_key)
        self.assertEqual(
            restored["update_reservation_candidate_references"],
            candidate_references,
        )
        self.assertEqual(
            restored["update_reservation_stage"],
            "select_reservation_reference",
        )
        self.assertEqual(restored["update_reservation_page_cursor"], page_cursor)
        self.assertFalse(restored["update_reservation_page_has_more"])

    def test_12_selectable_keyset_pages_reach_all_rows_without_remapping(self):
        owner = self._owner()
        inserted = []
        with self.Session.begin() as db:
            for index in range(10):
                row = Reservation(
                    owner_customer_id=owner,
                    public_reference=f"RSV_{uuid4().hex}",
                    name=f"Page {index}",
                    people=index + 1,
                    date="2026-08-15",
                    time="19:00",
                    status="pending",
                )
                db.add(row)
                db.flush()
                inserted.append((row.id, row.public_reference))

        service = ReservationService()
        with self.Session() as db:
            first = service.list_selectable_reservation_page(
                db,
                owner_customer_id=owner,
                page_size=5,
            )
        self.assertTrue(first.has_more)
        self.assertEqual(
            [row.id for row in first.reservations],
            [identifier for identifier, _reference in reversed(inserted[5:])],
        )

        with self.Session.begin() as db:
            db.add(
                Reservation(
                    owner_customer_id=owner,
                    public_reference=f"RSV_{uuid4().hex}",
                    name="Created after first page",
                    people=2,
                    date="2026-08-16",
                    time="20:00",
                    status="pending",
                )
            )

        with self.Session() as db:
            second = service.list_selectable_reservation_page(
                db,
                owner_customer_id=owner,
                after_public_reference=first.reservations[-1].reference,
                page_size=5,
            )
        self.assertFalse(second.has_more)
        self.assertEqual(
            [row.id for row in second.reservations],
            [identifier for identifier, _reference in reversed(inserted[:5])],
        )


if __name__ == "__main__":
    unittest.main()
