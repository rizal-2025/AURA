"""Disposable PostgreSQL verification for isolated demo persistence."""

from datetime import datetime, timedelta, timezone
import os
import re
import unittest
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.transaction_errors import PersistenceOperationError
from app.core.unit_of_work import UnitOfWork
from app.db.models.conversation_workflow_state import ConversationWorkflowState
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
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
    DemoRateLimitBucketRepository,
    DemoSessionRepository,
)
from migrations.add_demo_persistence import (
    CHECK_CONSTRAINTS,
    DEMO_TABLES,
    DemoPersistenceMigrationError,
    FOREIGN_KEYS,
    INDEXES,
    PRIMARY_KEYS,
    UNIQUE_CONSTRAINTS,
    migrate,
)
from migrations.add_demo_chat_request_id import (
    REQUEST_ID_INDEXES,
    migrate as migrate_request_id,
)
from migrations.add_demo_chat_reservation_mutation import (
    DemoChatReservationMutationMigrationError,
    MUTATION_CONSTRAINTS,
    _constraint_is_compatible as mutation_constraint_is_compatible,
    migrate as migrate_reservation_mutation,
)
from tests.integration.disposable_schema import DisposableSchemaResources


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return (
            "TEST_DATABASE_URL is not configured; demo persistence "
            "PostgreSQL tests are skipped."
        )
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if parsed.database != "aura_test":
            return "TEST_DATABASE_URL must target the exact aura_test database."
    except Exception:
        return "TEST_DATABASE_URL is invalid; PostgreSQL tests are skipped."
    return None


SKIP_REASON = _skip_reason()

MODELS_BY_TABLE = {
    model.__tablename__: model
    for model in (
        DemoSession,
        DemoChatMessage,
        DemoHandoffEvent,
        DemoRateLimitBucket,
    )
}

# The migration deliberately supplies these database-side defaults. Model
# metadata may use equivalent Python-side defaults and is not required to
# duplicate them as server_default declarations.
EXPECTED_SERVER_DEFAULTS = {
    "demo_sessions": {
        "id": "generated_identifier",
        "environment_scope": "demo",
        "created_at": "current_timestamp",
        "last_seen_at": "current_timestamp",
        "updated_at": "current_timestamp",
    },
    "demo_chat_messages": {
        "id": "generated_identifier",
        "created_at": "current_timestamp",
    },
    "demo_handoff_events": {
        "id": "generated_identifier",
        "status": "simulated",
        "created_at": "current_timestamp",
    },
    "demo_rate_limit_buckets": {
        "id": "generated_identifier",
        "request_count": "zero",
        "updated_at": "current_timestamp",
    },
}


def _type_semantics(value):
    if isinstance(value, Text):
        return ("text",)
    if isinstance(value, String):
        return ("varchar", value.length)
    if (
        isinstance(value, Uuid)
        or value.__class__.__name__.casefold() == "uuid"
    ):
        return ("uuid",)
    if isinstance(value, DateTime):
        return ("timestamp", bool(value.timezone))
    if isinstance(value, Integer):
        return ("integer",)
    return (value.__class__.__name__.casefold(),)


def _check_has_semantics(
    value,
    *,
    expression: str,
    required_fragments: tuple[str, ...],
) -> bool:
    normalized = (
        str(value if value is not None else "")
        .casefold()
        .replace('"', "")
        .replace("::text", "")
        .replace("::character varying", "")
    )
    expected_literals = set(
        re.findall(r"'([^']+)'", expression.casefold())
    )
    actual_literals = set(re.findall(r"'([^']+)'", normalized))
    compact = re.sub(r"\s+", "", normalized)
    return (
        expected_literals == actual_literals
        and all(
            fragment.casefold().replace(" ", "") in compact
            for fragment in required_fragments
        )
        and " or " not in normalized
        and "true" not in normalized
    )


def _server_default_semantics(value):
    if value is None:
        return None
    normalized = (
        str(value)
        .casefold()
        .replace("::character varying", "")
        .replace("::integer", "")
        .strip()
    )
    if "nextval(" in normalized:
        return "generated_identifier"
    if "current_timestamp" in normalized or "now()" in normalized:
        return "current_timestamp"
    if re.fullmatch(r"\(*'demo'\)*", normalized):
        return "demo"
    if re.fullmatch(r"\(*'simulated'\)*", normalized):
        return "simulated"
    if re.fullmatch(r"\(*0\)*", normalized):
        return "zero"
    return "unexpected"


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class DemoPersistencePostgreSQLTests(unittest.TestCase):
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

        cls.schema = f"aura_demo_persistence_test_{uuid4().hex[:12]}"
        cls.resources = DisposableSchemaResources(
            admin_engine=cls.admin,
            schema=cls.schema,
            allowed_prefixes=("aura_demo_persistence_test_",),
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
        cls._create_core_tables(cls.engine, include_ticket_tables=True)
        inspector = inspect(cls.engine)
        cls.core_columns_before = {
            table_name: tuple(
                item["name"]
                for item in inspector.get_columns(
                    table_name,
                    schema=cls.schema,
                )
            )
            for table_name in (
                "customers",
                "reservations",
                "conversation_workflow_states",
            )
        }
        cls.initial_migration_changed = migrate(
            cls.engine,
            schema=cls.schema,
        )
        cls.initial_request_migration_changed = migrate_request_id(
            cls.engine,
            schema=cls.schema,
        )
        cls.initial_mutation_migration_changed = migrate_reservation_mutation(
            cls.engine,
            schema=cls.schema,
        )

    @classmethod
    def _table(cls, name: str) -> str:
        return f'"{cls.schema}"."{name}"'

    @staticmethod
    def _schema_table(schema: str, name: str) -> str:
        return f'"{schema}"."{name}"'

    @classmethod
    def _new_disposable_engine(cls):
        schema = f"aura_demo_persistence_test_{uuid4().hex[:12]}"
        cls.resources.track_schema(schema)
        with cls.admin.begin() as connection:
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
        cls.resources.track_engine(engine)
        return schema, engine

    @staticmethod
    def _create_core_tables(engine, *, include_ticket_tables=False):
        Customer.__table__.create(engine)
        Reservation.__table__.create(engine)
        ConversationWorkflowState.__table__.create(engine)
        if include_ticket_tables:
            SupportTicket.__table__.create(engine)
            SupportTicketNotification.__table__.create(engine)

    @classmethod
    def _core_signature(cls, engine, schema: str):
        inspector = inspect(engine)
        table_names = (
            "customers",
            "reservations",
            "conversation_workflow_states",
        )
        with engine.connect() as connection:
            return {
                table_name: (
                    tuple(
                        item["name"]
                        for item in inspector.get_columns(
                            table_name,
                            schema=schema,
                        )
                    ),
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM "
                            f"{cls._schema_table(schema, table_name)}"
                        )
                    ),
                )
                for table_name in table_names
            }

    def _assert_integrity_error_has_no_connection_secret(self, error):
        output = str(error) + repr(error)
        parsed = make_url(os.environ["TEST_DATABASE_URL"])
        markers = [
            parsed.render_as_string(hide_password=False),
            parsed.username,
            parsed.password,
        ]
        leaked = any(
            marker and len(marker) >= 4 and marker in output
            for marker in markers
        )
        self.assertFalse(
            leaked,
            "Integrity error exposed database connection credentials.",
        )
        self.assertNotIn("postgresql://", output.casefold())

    def _assert_safe_migration_error(self, error):
        output = str(error) + repr(error)
        fake_markers = (
            "fake-user-marker",
            "fake-password-marker",
            "sslmode=fake-marker",
            (
                "postgresql://fake-user-marker:fake-password-marker@"
                "localhost/aura_test?sslmode=fake-marker"
            ),
        )
        self.assertEqual(
            str(error),
            "Demo persistence migration failed safely.",
        )
        self.assertFalse(
            any(marker in output for marker in fake_markers),
            "Safe migration error exposed credential markers.",
        )
        self.assertNotIn("alter table", output.casefold())
        self.assertNotIn("postgresql://", output.casefold())

    @staticmethod
    def _digest(seed: int) -> str:
        return f"{seed:064x}"

    def _owner(self, db):
        owner = Customer()
        db.add(owner)
        db.flush()
        return owner

    def _session(self, db, seed: int, *, now=None):
        timestamp = now or datetime.now(timezone.utc)
        owner = self._owner(db)
        row = DemoSessionRepository().create(
            db,
            token_digest=self._digest(seed),
            owner_customer_id=owner.id,
            idle_expires_at=timestamp + timedelta(minutes=30),
            absolute_expires_at=timestamp + timedelta(hours=2),
            now=timestamp,
        )
        return row, owner

    def _prepare_pre_phase_c_schema(self):
        schema, engine = self._new_disposable_engine()
        self._create_core_tables(engine)
        self.assertTrue(migrate(engine, schema=schema))
        self.assertTrue(migrate_request_id(engine, schema=schema))
        return schema, engine

    @classmethod
    def _phase_c_signature(cls, engine, schema):
        inspector = inspect(engine)
        columns = {
            item["name"]: (
                _type_semantics(item["type"]),
                bool(item.get("nullable")),
                item.get("default"),
                item.get("identity"),
                item.get("computed"),
            )
            for item in inspector.get_columns(
                "demo_chat_messages",
                schema=schema,
            )
            if item["name"].startswith("reservation_mutation_")
        }
        checks = {
            item.get("name"): item.get("sqltext")
            for item in inspector.get_check_constraints(
                "demo_chat_messages",
                schema=schema,
            )
            if "reservation_mutation" in str(item.get("name"))
        }
        with engine.connect() as connection:
            result = connection.execute(
                text(
                    """
                    SELECT constraint_row.conname, constraint_row.convalidated
                    FROM pg_catalog.pg_constraint AS constraint_row
                    JOIN pg_catalog.pg_class AS relation
                      ON relation.oid = constraint_row.conrelid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE constraint_row.contype = 'c'
                      AND relation.relname = 'demo_chat_messages'
                      AND namespace.nspname = :schema_name
                      AND constraint_row.conname LIKE
                          'ck_demo_chat_messages_reservation_mutation%'
                    """
                ),
                {"schema_name": schema},
            )
            validated = {row[0]: bool(row[1]) for row in result}
        return columns, checks, validated

    def _assert_phase_c_rejected_without_rewrite(self, engine, schema):
        before = self._phase_c_signature(engine, schema)
        with self.assertRaises(
            DemoChatReservationMutationMigrationError
        ) as captured:
            migrate_reservation_mutation(engine, schema=schema)
        self.assertEqual(
            str(captured.exception),
            "Demo chat reservation mutation migration failed safely.",
        )
        self.assertNotIn("alter table", repr(captured.exception).casefold())
        self.assertEqual(self._phase_c_signature(engine, schema), before)

    def test_01_migration_creates_four_tables_and_is_idempotent(self):
        self.assertTrue(self.initial_migration_changed)
        self.assertTrue(self.initial_request_migration_changed)
        self.assertTrue(self.initial_mutation_migration_changed)
        self.assertFalse(migrate(self.engine, schema=self.schema))
        self.assertFalse(
            migrate_request_id(self.engine, schema=self.schema)
        )
        self.assertFalse(
            migrate_reservation_mutation(self.engine, schema=self.schema)
        )
        inspector = inspect(self.engine)
        for table_name in DEMO_TABLES:
            with self.subTest(table_name=table_name):
                self.assertTrue(
                    inspector.has_table(table_name, schema=self.schema)
                )

    def test_02_constraints_and_indexes_are_available(self):
        inspector = inspect(self.engine)
        for table_name in DEMO_TABLES:
            with self.subTest(table_name=table_name):
                primary_key = inspector.get_pk_constraint(
                    table_name,
                    schema=self.schema,
                )
                self.assertEqual(
                    primary_key.get("name"),
                    PRIMARY_KEYS[table_name][0],
                )
                foreign_key_names = {
                    item.get("name")
                    for item in inspector.get_foreign_keys(
                        table_name,
                        schema=self.schema,
                    )
                }
                self.assertTrue({
                    item[0] for item in FOREIGN_KEYS[table_name]
                }.issubset(foreign_key_names))
                unique_names = {
                    item.get("name")
                    for item in inspector.get_unique_constraints(
                        table_name,
                        schema=self.schema,
                    )
                }
                self.assertTrue({
                    item[0] for item in UNIQUE_CONSTRAINTS[table_name]
                }.issubset(unique_names))
                check_names = {
                    item.get("name")
                    for item in inspector.get_check_constraints(
                        table_name,
                        schema=self.schema,
                    )
                }
                self.assertTrue({
                    item[0] for item in CHECK_CONSTRAINTS[table_name]
                }.issubset(check_names))
                index_names = {
                    item.get("name")
                    for item in inspector.get_indexes(
                        table_name,
                        schema=self.schema,
                    )
                }
                self.assertTrue({
                    item[0] for item in INDEXES[table_name]
                }.issubset(index_names))

    def test_02a_mutation_constraints_accept_only_safe_assistant_pairs(self):
        with self.Session() as db:
            session, _owner = self._session(db, 8201)
            db.commit()
            session_id = session.id

        statement = text(
            f"INSERT INTO {self._table('demo_chat_messages')} "
            "(demo_session_id, role, content, request_id, "
            "reservation_mutation_operation, "
            "reservation_mutation_reference, created_at) "
            "VALUES (:session_id, :role, 'safe', :request_id, "
            ":operation, :reference, :created_at)"
        )
        reference = "RSV_" + ("d" * 32)
        now = datetime.now(timezone.utc)
        accepted = (
            ("assistant", None, None),
            ("assistant", "created", reference),
        )
        for role, operation, persisted_reference in accepted:
            with self.engine.begin() as connection:
                connection.execute(
                    statement,
                    {
                        "session_id": session_id,
                        "role": role,
                        "request_id": str(uuid4()),
                        "operation": operation,
                        "reference": persisted_reference,
                        "created_at": now,
                    },
                )

        rejected = (
            ("user", "created", reference),
            ("assistant", "created", None),
            ("assistant", None, reference),
            ("assistant", "unknown", reference),
            ("assistant", "created", "RSV_" + ("D" * 32)),
            ("assistant", "created", "BAD_" + ("d" * 32)),
        )
        for role, operation, persisted_reference in rejected:
            with self.subTest(role=role, operation=operation):
                with self.assertRaises(IntegrityError):
                    with self.engine.begin() as connection:
                        connection.execute(
                            statement,
                            {
                                "session_id": session_id,
                                "role": role,
                                "request_id": str(uuid4()),
                                "operation": operation,
                                "reference": persisted_reference,
                                "created_at": now,
                            },
                        )

    def test_02b_partial_phase_c_schema_fails_closed_without_second_column(self):
        schema, engine = self._new_disposable_engine()
        self._create_core_tables(engine)
        migrate(engine, schema=schema)
        migrate_request_id(engine, schema=schema)
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {self._schema_table(schema, 'demo_chat_messages')} "
                    "ADD COLUMN reservation_mutation_operation VARCHAR(16) NULL"
                )
            )

        with self.assertRaises(DemoChatReservationMutationMigrationError):
            migrate_reservation_mutation(engine, schema=schema)

        columns = {
            item["name"]
            for item in inspect(engine).get_columns(
                "demo_chat_messages",
                schema=schema,
            )
        }
        self.assertIn("reservation_mutation_operation", columns)
        self.assertNotIn("reservation_mutation_reference", columns)

    def test_02c_competing_phase_c_constraint_is_rejected_without_rewrite(self):
        schema, engine = self._new_disposable_engine()
        self._create_core_tables(engine)
        migrate(engine, schema=schema)
        migrate_request_id(engine, schema=schema)
        table = self._schema_table(schema, "demo_chat_messages")
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN reservation_mutation_operation VARCHAR(16) NULL, "
                    "ADD COLUMN reservation_mutation_reference VARCHAR(36) NULL, "
                    "ADD CONSTRAINT ck_demo_chat_messages_competing_mutation "
                    "CHECK (reservation_mutation_operation IS NULL OR "
                    "reservation_mutation_operation = 'created')"
                )
            )

        with self.assertRaises(DemoChatReservationMutationMigrationError):
            migrate_reservation_mutation(engine, schema=schema)

        check_names = {
            item.get("name")
            for item in inspect(engine).get_check_constraints(
                "demo_chat_messages",
                schema=schema,
            )
        }
        self.assertIn(
            "ck_demo_chat_messages_competing_mutation",
            check_names,
        )
        self.assertTrue(set(MUTATION_CONSTRAINTS).isdisjoint(check_names))

    def test_02d_wrong_phase_c_column_length_is_rejected_without_rewrite(self):
        schema, engine = self._new_disposable_engine()
        self._create_core_tables(engine)
        migrate(engine, schema=schema)
        migrate_request_id(engine, schema=schema)
        table = self._schema_table(schema, "demo_chat_messages")
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN reservation_mutation_operation VARCHAR(15) NULL, "
                    "ADD COLUMN reservation_mutation_reference VARCHAR(36) NULL"
                )
            )

        with self.assertRaises(DemoChatReservationMutationMigrationError):
            migrate_reservation_mutation(engine, schema=schema)

        columns = {
            item["name"]: item
            for item in inspect(engine).get_columns(
                "demo_chat_messages",
                schema=schema,
            )
        }
        self.assertEqual(columns["reservation_mutation_operation"]["type"].length, 15)

    def test_02e_phase_c_column_metadata_must_be_exact(self):
        cases = {
            "operation_wrong_type": (
                "reservation_mutation_operation TEXT NULL",
                "reservation_mutation_reference VARCHAR(36) NULL",
            ),
            "reference_wrong_type": (
                "reservation_mutation_operation VARCHAR(16) NULL",
                "reservation_mutation_reference TEXT NULL",
            ),
            "reference_wrong_length": (
                "reservation_mutation_operation VARCHAR(16) NULL",
                "reservation_mutation_reference VARCHAR(35) NULL",
            ),
            "operation_not_null": (
                "reservation_mutation_operation VARCHAR(16) NOT NULL",
                "reservation_mutation_reference VARCHAR(36) NULL",
            ),
            "reference_not_null": (
                "reservation_mutation_operation VARCHAR(16) NULL",
                "reservation_mutation_reference VARCHAR(36) NOT NULL",
            ),
            "empty_default": (
                "reservation_mutation_operation VARCHAR(16) NULL DEFAULT ''",
                "reservation_mutation_reference VARCHAR(36) NULL",
            ),
            "constant_default": (
                "reservation_mutation_operation VARCHAR(16) NULL DEFAULT 'created'",
                "reservation_mutation_reference VARCHAR(36) NULL",
            ),
            "function_default": (
                "reservation_mutation_operation VARCHAR(16) NULL "
                "DEFAULT lower('created')",
                "reservation_mutation_reference VARCHAR(36) NULL",
            ),
            "casted_default": (
                "reservation_mutation_operation VARCHAR(16) NULL "
                "DEFAULT 'created'::VARCHAR",
                "reservation_mutation_reference VARCHAR(36) NULL",
            ),
            "explicit_null_default": (
                "reservation_mutation_operation VARCHAR(16) NULL DEFAULT NULL",
                "reservation_mutation_reference VARCHAR(36) NULL",
            ),
            "generated_reference_default": (
                "reservation_mutation_operation VARCHAR(16) NULL",
                "reservation_mutation_reference VARCHAR(36) NULL DEFAULT "
                "('RSV_' || repeat('a', 32))",
            ),
            "identity_metadata": (
                "reservation_mutation_operation INTEGER GENERATED ALWAYS AS IDENTITY",
                "reservation_mutation_reference VARCHAR(36) NULL",
            ),
            "computed_metadata": (
                "reservation_mutation_operation VARCHAR(16) NULL",
                "reservation_mutation_reference VARCHAR(36) GENERATED ALWAYS AS "
                "('RSV_' || repeat('a', 32)) STORED",
            ),
        }
        for name, definitions in cases.items():
            with self.subTest(case=name):
                schema, engine = self._prepare_pre_phase_c_schema()
                table = self._schema_table(schema, "demo_chat_messages")
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            f"ALTER TABLE {table} "
                            f"ADD COLUMN {definitions[0]}, "
                            f"ADD COLUMN {definitions[1]}"
                        )
                    )
                self._assert_phase_c_rejected_without_rewrite(engine, schema)

    def test_02f_unexpected_related_column_is_rejected(self):
        schema, engine = self._prepare_pre_phase_c_schema()
        table = self._schema_table(schema, "demo_chat_messages")
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN reservation_mutation_operation VARCHAR(16) NULL, "
                    "ADD COLUMN reservation_mutation_reference VARCHAR(36) NULL, "
                    "ADD COLUMN reservation_mutation_payload JSONB NULL"
                )
            )
        self._assert_phase_c_rejected_without_rewrite(engine, schema)

    def test_02g_malformed_and_unvalidated_phase_c_constraints_fail_closed(self):
        operation = "reservation_mutation_operation"
        reference = "reservation_mutation_reference"
        cases = (
            ("operation", f"{operation} IS NULL OR {operation} IN ('created', 'updated', 'cancelled', 'deleted')", False),
            ("operation", f"{operation} IS NULL OR {operation} IN ('created', 'updated')", False),
            ("operation", f"{operation} IS NULL OR lower({operation}) = 'created'", False),
            ("operation", f"{operation} IS NULL OR {operation} LIKE '%'", False),
            ("operation", f"{operation} IS NULL OR TRUE", False),
            ("operation", f"{reference} IS NULL OR {reference} = 'created'", False),
            ("pair", f"{operation} IS NULL OR {reference} IS NOT NULL", False),
            ("pair", f"{operation} IS NULL OR {reference} IS NULL", False),
            ("pair", f"{operation} IS NULL OR role IS NULL", False),
            ("pair", "TRUE", False),
            ("assistant_role", f"{operation} IS NULL OR role IN ('assistant', 'user')", False),
            ("assistant_role", f"{operation} IS NULL OR role = 'user'", False),
            ("assistant_role", f"{operation} IS NULL OR lower(role) = 'assistant'", False),
            ("assistant_role", f"{reference} IS NULL OR role = 'assistant'", False),
            ("reference", f"{reference} IS NULL OR {reference} ~* '^RSV_[0-9a-f]{{32}}$'", False),
            ("reference", f"{reference} IS NULL OR {reference} ~ '^BAD_[0-9a-f]{{32}}$'", False),
            ("reference", f"{reference} IS NULL OR {reference} ~ '^RSV_[0-9a-f]{{31}}$'", False),
            ("reference", f"{reference} IS NULL OR {reference} ~ '^RSV_[0-9a-z]{{32}}$'", False),
            ("reference", f"{reference} IS NULL OR {reference} LIKE 'RSV_%'", False),
            ("reference", f"{reference} IS NULL OR substring({reference}, 1, 4) = 'RSV_'", False),
            ("reference", f"{operation} IS NULL OR {operation} ~ '^RSV_[0-9a-f]{{32}}$'", False),
            ("reference", "TRUE", False),
            ("reference", MUTATION_CONSTRAINTS["ck_demo_chat_messages_reservation_mutation_reference"], True),
        )
        for suffix, expression, not_valid in cases:
            with self.subTest(suffix=suffix, not_valid=not_valid, size=len(expression)):
                schema, engine = self._prepare_pre_phase_c_schema()
                table = self._schema_table(schema, "demo_chat_messages")
                constraint = f"ck_demo_chat_messages_reservation_mutation_{suffix}"
                validation = " NOT VALID" if not_valid else ""
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            f"ALTER TABLE {table} "
                            "ADD COLUMN reservation_mutation_operation VARCHAR(16) NULL, "
                            "ADD COLUMN reservation_mutation_reference VARCHAR(36) NULL, "
                            f"ADD CONSTRAINT {constraint} CHECK ({expression}){validation}"
                        )
                    )
                self._assert_phase_c_rejected_without_rewrite(engine, schema)

    def test_02h_old_rows_are_preserved_without_backfill(self):
        schema, engine = self._prepare_pre_phase_c_schema()
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        request_id = uuid4()
        with Session() as db:
            session, _owner = self._session(db, 8291, now=now)
            db.commit()
            session_id = session.id
        table = self._schema_table(schema, "demo_chat_messages")
        with engine.begin() as connection:
            inserted = connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(demo_session_id, role, content, request_id, created_at) VALUES "
                    "(:session_id, 'user', :user_content, :request_id, :created_at), "
                    "(:session_id, 'assistant', :assistant_content, :request_id, :created_at) "
                    "RETURNING id, demo_session_id, role, content, request_id, created_at"
                ),
                {
                    "session_id": session_id,
                    "user_content": "legacy user content",
                    "assistant_content": "legacy assistant content",
                    "request_id": str(request_id),
                    "created_at": now,
                },
            ).all()
        self.assertTrue(migrate_reservation_mutation(engine, schema=schema))
        with engine.connect() as connection:
            preserved = connection.execute(
                text(
                    f"SELECT id, demo_session_id, role, content, request_id, created_at, "
                    "reservation_mutation_operation, reservation_mutation_reference "
                    f"FROM {table} ORDER BY id"
                )
            ).all()
        self.assertEqual(len(preserved), len(inserted))
        self.assertEqual([tuple(row[:6]) for row in preserved], [tuple(row) for row in inserted])
        self.assertTrue(all(tuple(row[6:]) == (None, None) for row in preserved))

    def test_02i_full_migration_chain_preserves_phase_c_row(self):
        schema, engine = self._prepare_pre_phase_c_schema()
        self.assertTrue(migrate_reservation_mutation(engine, schema=schema))
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        reference = "RSV_" + ("e" * 32)
        with Session() as db:
            session, _owner = self._session(db, 8292, now=now)
            db.commit()
            session_id = session.id
        table = self._schema_table(schema, "demo_chat_messages")
        request_id = uuid4()
        with engine.begin() as connection:
            inserted = connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(demo_session_id, role, content, request_id, "
                    "reservation_mutation_operation, reservation_mutation_reference, "
                    "created_at) VALUES "
                    "(:session_id, 'assistant', :content, :request_id, "
                    "'updated', :reference, :created_at) RETURNING id"
                ),
                {
                    "session_id": session_id,
                    "content": "persisted completion",
                    "request_id": str(request_id),
                    "reference": reference,
                    "created_at": now,
                },
            ).scalar_one()
        before = self._phase_c_signature(engine, schema)
        self.assertFalse(migrate(engine, schema=schema))
        self.assertFalse(migrate_reservation_mutation(engine, schema=schema))
        self.assertEqual(self._phase_c_signature(engine, schema), before)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT id, reservation_mutation_operation, "
                    f"reservation_mutation_reference FROM {table} WHERE id = :id"
                ),
                {"id": inserted},
            ).one()
        self.assertEqual(tuple(row), (inserted, "updated", reference))

    def test_03_model_metadata_and_migrated_schema_converge_semantically(self):
        inspector = inspect(self.engine)
        for table_name, model in MODELS_BY_TABLE.items():
            with self.subTest(table_name=table_name):
                table = model.__table__
                migrated_columns = {
                    item["name"]: item
                    for item in inspector.get_columns(
                        table_name,
                        schema=self.schema,
                    )
                }
                self.assertEqual(
                    tuple(migrated_columns),
                    tuple(table.columns.keys()),
                )
                for column in table.columns:
                    migrated = migrated_columns[column.name]
                    self.assertEqual(
                        _type_semantics(migrated["type"]),
                        _type_semantics(column.type),
                    )
                    self.assertEqual(
                        bool(migrated.get("nullable")),
                        bool(column.nullable),
                    )

                migrated_pk = inspector.get_pk_constraint(
                    table_name,
                    schema=self.schema,
                )
                model_pk_columns = tuple(
                    column.name for column in table.primary_key.columns
                )
                self.assertEqual(
                    (
                        migrated_pk.get("name"),
                        tuple(
                            migrated_pk.get("constrained_columns") or ()
                        ),
                    ),
                    (PRIMARY_KEYS[table_name][0], model_pk_columns),
                )

                model_foreign_keys = {
                    (
                        constraint.name,
                        tuple(
                            column.name for column in constraint.columns
                        ),
                        tuple(constraint.elements)[0].column.table.name,
                        tuple(
                            element.column.name
                            for element in constraint.elements
                        ),
                        constraint.ondelete,
                    )
                    for constraint in table.foreign_key_constraints
                }
                migrated_foreign_keys = {
                    (
                        item.get("name"),
                        tuple(item.get("constrained_columns") or ()),
                        item.get("referred_table"),
                        tuple(item.get("referred_columns") or ()),
                        (item.get("options") or {}).get("ondelete"),
                    )
                    for item in inspector.get_foreign_keys(
                        table_name,
                        schema=self.schema,
                    )
                }
                self.assertEqual(
                    migrated_foreign_keys,
                    model_foreign_keys,
                )
                self.assertTrue(
                    all(
                        foreign_key[-1] is None
                        for foreign_key in migrated_foreign_keys
                    )
                )

                model_uniques = {
                    (
                        constraint.name,
                        tuple(
                            column.name for column in constraint.columns
                        ),
                    )
                    for constraint in table.constraints
                    if isinstance(constraint, UniqueConstraint)
                }
                migrated_uniques = {
                    (
                        item.get("name"),
                        tuple(item.get("column_names") or ()),
                    )
                    for item in inspector.get_unique_constraints(
                        table_name,
                        schema=self.schema,
                    )
                }
                self.assertEqual(migrated_uniques, model_uniques)

                expected_checks = {
                    name: (expression, required_fragments)
                    for name, expression, required_fragments
                    in CHECK_CONSTRAINTS[table_name]
                }
                if table_name == "demo_chat_messages":
                    expected_checks.update(
                        {
                            name: (
                                expression,
                                (
                                    "reservation_mutation_operation",
                                    "reservation_mutation_reference",
                                ),
                            )
                            for name, expression in MUTATION_CONSTRAINTS.items()
                        }
                    )
                model_checks = {
                    constraint.name: constraint.sqltext
                    for constraint in table.constraints
                    if isinstance(constraint, CheckConstraint)
                }
                migrated_checks = {
                    item.get("name"): item.get("sqltext")
                    for item in inspector.get_check_constraints(
                        table_name,
                        schema=self.schema,
                    )
                }
                self.assertEqual(
                    set(model_checks),
                    set(expected_checks),
                )
                self.assertEqual(
                    set(migrated_checks),
                    set(expected_checks),
                )
                for name, (
                    expression,
                    required_fragments,
                ) in expected_checks.items():
                    if name in MUTATION_CONSTRAINTS:
                        self.assertTrue(
                            mutation_constraint_is_compatible(
                                name,
                                str(model_checks[name]),
                            )
                        )
                        self.assertTrue(
                            mutation_constraint_is_compatible(
                                name,
                                migrated_checks[name],
                            )
                        )
                        continue
                    self.assertTrue(
                        _check_has_semantics(
                            model_checks[name],
                            expression=expression,
                            required_fragments=required_fragments,
                        )
                    )
                    self.assertTrue(
                        _check_has_semantics(
                            migrated_checks[name],
                            expression=expression,
                            required_fragments=required_fragments,
                        )
                    )

                model_indexes = {
                    (
                        index.name,
                        tuple(
                            expression.name
                            for expression in index.expressions
                        ),
                        bool(index.unique),
                    )
                    for index in table.indexes
                }
                required_index_names = {
                    index.name for index in table.indexes
                }
                migrated_indexes = {
                    (
                        item.get("name"),
                        tuple(item.get("column_names") or ()),
                        bool(item.get("unique")),
                    )
                    for item in inspector.get_indexes(
                        table_name,
                        schema=self.schema,
                    )
                    if item.get("name") in required_index_names
                }
                self.assertEqual(migrated_indexes, model_indexes)

                if table_name == "demo_chat_messages":
                    migrated_by_name = {
                        item.get("name"): item
                        for item in inspector.get_indexes(
                            table_name,
                            schema=self.schema,
                        )
                    }
                    for name, predicate in REQUEST_ID_INDEXES.items():
                        migrated = migrated_by_name[name]
                        model_index = next(
                            index
                            for index in table.indexes
                            if index.name == name
                        )
                        self.assertTrue(migrated.get("unique"))
                        self.assertEqual(
                            tuple(migrated.get("column_names") or ()),
                            ("demo_session_id", "request_id"),
                        )
                        self.assertIn(
                            predicate,
                            str(
                                model_index.dialect_options[
                                    "postgresql"
                                ]["where"]
                            ),
                        )

                actual_defaults = {
                    name: _server_default_semantics(
                        column.get("default")
                    )
                    for name, column in migrated_columns.items()
                    if column.get("default") is not None
                }
                self.assertEqual(
                    actual_defaults,
                    EXPECTED_SERVER_DEFAULTS[table_name],
                )
                rendered_defaults = " ".join(
                    str(column.get("default") or "")
                    for column in migrated_columns.values()
                ).casefold()
                self.assertNotIn("postgresql://", rendered_defaults)
                self.assertNotIn("password", rendered_defaults)

    def test_04_migration_does_not_change_core_tables(self):
        inspector = inspect(self.engine)
        for table_name, columns_before in self.core_columns_before.items():
            with self.subTest(table_name=table_name):
                columns_after = tuple(
                    item["name"]
                    for item in inspector.get_columns(
                        table_name,
                        schema=self.schema,
                    )
                )
                self.assertEqual(columns_after, columns_before)

    def test_05_database_constraints_reject_invalid_demo_rows(self):
        now = datetime.now(timezone.utc)
        with self.Session.begin() as db:
            session, _owner = self._session(db, 101, now=now)
            session_id = session.id

        invalid_statements = (
            (
                "chat role",
                text(
                    f"INSERT INTO {self._table('demo_chat_messages')} "
                    "(demo_session_id, role, content, created_at) "
                    "VALUES (:session_id, 'system', 'blocked', :now)"
                ),
                {"session_id": session_id, "now": now},
            ),
            (
                "handoff status",
                text(
                    f"INSERT INTO {self._table('demo_handoff_events')} "
                    "(demo_session_id, reference, status, reason_code, "
                    "created_at) VALUES "
                    "(:session_id, 'DEMO-HO-INVALID', 'open', "
                    "'internal_error', :now)"
                ),
                {"session_id": session_id, "now": now},
            ),
            (
                "negative count",
                text(
                    f"INSERT INTO {self._table('demo_rate_limit_buckets')} "
                    "(scope_type, subject_digest, action, window_started_at, "
                    "window_seconds, request_count, expires_at, updated_at) "
                    "VALUES ('session', :digest, 'chat.send', :now, "
                    "60, -1, :expires, :now)"
                ),
                {
                    "digest": self._digest(102),
                    "now": now,
                    "expires": now + timedelta(minutes=1),
                },
            ),
        )
        for label, statement, parameters in invalid_statements:
            with self.subTest(label=label):
                with self.assertRaises(IntegrityError):
                    with self.engine.begin() as connection:
                        connection.execute(statement, parameters)

    def test_06_repository_round_trip_remains_session_scoped(self):
        now = datetime.now(timezone.utc)
        with self.Session.begin() as db:
            first, first_owner = self._session(db, 103, now=now)
            second, second_owner = self._session(db, 104, now=now)
            messages = DemoChatMessageRepository()
            handoffs = DemoHandoffEventRepository()
            messages.append(
                db,
                demo_session_id=first.id,
                role="user",
                content="first-only",
                created_at=now,
            )
            messages.append(
                db,
                demo_session_id=second.id,
                role="assistant",
                content="second-only",
                created_at=now,
            )
            handoffs.create_simulated(
                db,
                demo_session_id=first.id,
                reference="DEMO-HO-PG-FIRST",
                reason_code="internal_error",
                created_at=now,
            )
            first_id = first.id
            second_id = second.id
            first_owner_id = first_owner.id
            second_owner_id = second_owner.id

        with self.Session() as db:
            first_messages = DemoChatMessageRepository().list_latest(
                db,
                demo_session_id=first_id,
            )
            second_events = DemoHandoffEventRepository().list_latest(
                db,
                demo_session_id=second_id,
            )
            self.assertEqual(
                [row.content for row in first_messages],
                ["first-only"],
            )
            self.assertEqual(second_events, [])
            self.assertNotEqual(first_owner_id, second_owner_id)

    def test_07_transaction_rollback_leaves_no_partial_rows(self):
        now = datetime.now(timezone.utc)
        with self.Session.begin() as db:
            owner = self._owner(db)
            owner_id = owner.id

        with self.Session() as db:
            with self.assertRaises(PersistenceOperationError):
                with UnitOfWork(db):
                    session = DemoSessionRepository().create(
                        db,
                        token_digest=self._digest(105),
                        owner_customer_id=owner_id,
                        idle_expires_at=now + timedelta(minutes=30),
                        absolute_expires_at=now + timedelta(hours=2),
                        now=now,
                    )
                    DemoChatMessageRepository().append(
                        db,
                        demo_session_id=session.id,
                        role="user",
                        content="rollback",
                        created_at=now,
                    )
                    raise RuntimeError("forced rollback")

        with self.Session() as db:
            self.assertIsNone(
                DemoSessionRepository().get_by_token_digest(
                    db,
                    token_digest=self._digest(105),
                )
            )

    def test_08_missing_session_foreign_key_is_sanitized(self):
        now = datetime.now(timezone.utc)
        with self.Session() as db:
            with self.assertRaises(PersistenceOperationError) as raised:
                with UnitOfWork(db) as unit:
                    DemoChatMessageRepository().append(
                        db,
                        demo_session_id=999999,
                        role="user",
                        content="not-persisted",
                        created_at=now,
                    )
                    unit.commit()
        output = str(raised.exception) + repr(raised.exception)
        self.assertEqual(str(raised.exception), "PERSISTENCE_OPERATION_FAILED")
        self.assertNotIn("not-persisted", output)

    def test_09_missing_index_is_recreated_convergently(self):
        index_name = INDEXES["demo_chat_messages"][0][0]
        with self.engine.begin() as connection:
            connection.execute(
                text(f'DROP INDEX {self._table(index_name)}')
            )
        self.assertTrue(migrate(self.engine, schema=self.schema))
        self.assertFalse(migrate(self.engine, schema=self.schema))
        index_names = {
            item.get("name")
            for item in inspect(self.engine).get_indexes(
                "demo_chat_messages",
                schema=self.schema,
            )
        }
        self.assertIn(index_name, index_names)

    def test_10_duplicate_session_token_digest_is_rejected(self):
        now = datetime.now(timezone.utc)
        duplicate_digest = self._digest(201)
        with self.Session.begin() as db:
            first, _first_owner = self._session(db, 201, now=now)
            second_owner = self._owner(db)
            first_id = first.id
            second_owner_id = second_owner.id

        with self.Session() as db:
            with self.assertRaises(IntegrityError) as raised:
                DemoSessionRepository().create(
                    db,
                    token_digest=duplicate_digest,
                    owner_customer_id=second_owner_id,
                    idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=2),
                    now=now,
                )
            db.rollback()
        self._assert_integrity_error_has_no_connection_secret(
            raised.exception
        )

        with self.Session() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoSession)
                    .where(DemoSession.token_digest == duplicate_digest)
                ),
                1,
            )
            self.assertIsNotNone(db.get(DemoSession, first_id))
            self.assertIsNone(
                db.scalar(
                    select(DemoSession).where(
                        DemoSession.owner_customer_id == second_owner_id
                    )
                )
            )

    def test_11_duplicate_session_owner_is_rejected(self):
        now = datetime.now(timezone.utc)
        second_digest = self._digest(203)
        with self.Session.begin() as db:
            first, owner = self._session(db, 202, now=now)
            first_id = first.id
            owner_id = owner.id

        with self.Session() as db:
            with self.assertRaises(IntegrityError) as raised:
                DemoSessionRepository().create(
                    db,
                    token_digest=second_digest,
                    owner_customer_id=owner_id,
                    idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=2),
                    now=now,
                )
            db.rollback()
        self._assert_integrity_error_has_no_connection_secret(
            raised.exception
        )

        with self.Session() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoSession)
                    .where(DemoSession.owner_customer_id == owner_id)
                ),
                1,
            )
            self.assertIsNotNone(db.get(DemoSession, first_id))
            self.assertIsNone(
                DemoSessionRepository().get_by_token_digest(
                    db,
                    token_digest=second_digest,
                )
            )

    def test_12_duplicate_handoff_reference_is_rejected(self):
        now = datetime.now(timezone.utc)
        reference = "DEMO-HO-PG-DUPLICATE"
        with self.Session.begin() as db:
            first, _first_owner = self._session(db, 204, now=now)
            second, _second_owner = self._session(db, 205, now=now)
            DemoHandoffEventRepository().create_simulated(
                db,
                demo_session_id=first.id,
                reference=reference,
                reason_code="internal_error",
                created_at=now,
            )
            second_id = second.id

        with self.Session() as db:
            with self.assertRaises(IntegrityError) as raised:
                DemoHandoffEventRepository().create_simulated(
                    db,
                    demo_session_id=second_id,
                    reference=reference,
                    reason_code="internal_error",
                    created_at=now,
                )
            db.rollback()
        self._assert_integrity_error_has_no_connection_secret(
            raised.exception
        )

        with self.Session() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoHandoffEvent)
                    .where(DemoHandoffEvent.reference == reference)
                ),
                1,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(SupportTicket)
                ),
                0,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(SupportTicketNotification)
                ),
                0,
            )

    def test_13_duplicate_rate_limit_bucket_identity_is_rejected(self):
        now = datetime.now(timezone.utc)
        subject_digest = self._digest(206)
        repository = DemoRateLimitBucketRepository()
        identity = {
            "scope_type": "session",
            "subject_digest": subject_digest,
            "action": "chat.send",
            "window_started_at": now,
            "window_seconds": 60,
        }
        with self.Session.begin() as db:
            repository.create(
                db,
                **identity,
                request_count=1,
                expires_at=now + timedelta(minutes=1),
                now=now,
            )

        with self.Session() as db:
            with self.assertRaises(IntegrityError) as raised:
                repository.create(
                    db,
                    **identity,
                    request_count=2,
                    expires_at=now + timedelta(minutes=1),
                    now=now,
                )
            db.rollback()
        self._assert_integrity_error_has_no_connection_secret(
            raised.exception
        )

        with self.Session.begin() as db:
            repository.create(
                db,
                **{**identity, "action": "handoff.simulate"},
                request_count=1,
                expires_at=now + timedelta(minutes=1),
                now=now,
            )

        with self.Session() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(DemoRateLimitBucket)
                    .where(
                        DemoRateLimitBucket.scope_type
                        == identity["scope_type"],
                        DemoRateLimitBucket.subject_digest
                        == subject_digest,
                        DemoRateLimitBucket.window_started_at == now,
                        DemoRateLimitBucket.window_seconds == 60,
                    )
                ),
                2,
            )

    def test_14_incompatible_existing_schema_fails_closed(self):
        schema, engine = self._new_disposable_engine()
        self._create_core_tables(engine)
        before = self._core_signature(engine, schema)
        invalid_table = self._schema_table(
            schema,
            "demo_handoff_events",
        )
        with engine.begin() as connection:
            connection.execute(text(
                f"CREATE TABLE {invalid_table} ("
                "id SERIAL PRIMARY KEY, unexpected TEXT)"
            ))

        with self.assertRaises(DemoPersistenceMigrationError) as raised:
            migrate(engine, schema=schema)

        output = str(raised.exception) + repr(raised.exception)
        self.assertNotIn("postgresql://", output.casefold())
        self.assertNotIn("password", output.casefold())
        inspector = inspect(engine)
        self.assertEqual(
            {
                table_name
                for table_name in DEMO_TABLES
                if inspector.has_table(table_name, schema=schema)
            },
            {"demo_handoff_events"},
        )
        self.assertEqual(
            tuple(
                item["name"]
                for item in inspector.get_columns(
                    "demo_handoff_events",
                    schema=schema,
                )
            ),
            ("id", "unexpected"),
        )
        self.assertEqual(
            self._core_signature(engine, schema),
            before,
        )

    def _assert_old_migration_rejects_wrong_request_predicate(self, role):
        schema, engine = self._new_disposable_engine()
        self._create_core_tables(engine)
        self.assertTrue(migrate(engine, schema=schema))
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

        with self.assertRaises(DemoPersistenceMigrationError) as captured:
            migrate(engine, schema=schema)

        rendered = str(captured.exception) + repr(captured.exception)
        self.assertNotIn("postgresql://", rendered.casefold())
        self.assertNotIn("password", rendered.casefold())

    def test_old_migration_rejects_wrong_user_predicate_only(self):
        self._assert_old_migration_rejects_wrong_request_predicate("user")

    def test_old_migration_rejects_wrong_assistant_predicate_only(self):
        self._assert_old_migration_rejects_wrong_request_predicate(
            "assistant"
        )

    def test_15_partial_migration_failure_rolls_back_all_new_ddl(self):
        schema, engine = self._new_disposable_engine()
        self._create_core_tables(engine)
        Session = sessionmaker(bind=engine)
        with Session.begin() as db:
            first_owner = Customer()
            second_owner = Customer()
            db.add_all((first_owner, second_owner))
            db.flush()
            owner_ids = (first_owner.id, second_owner.id)

        session_table = self._schema_table(schema, "demo_sessions")
        now = datetime.now(timezone.utc)
        duplicate_digest = self._digest(207)
        with engine.begin() as connection:
            connection.execute(text(f"""
                CREATE TABLE {session_table} (
                    id SERIAL NOT NULL,
                    token_digest VARCHAR(64) NOT NULL,
                    owner_customer_id UUID NOT NULL,
                    environment_scope VARCHAR(16) NOT NULL DEFAULT 'demo',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    idle_expires_at TIMESTAMPTZ NOT NULL,
                    absolute_expires_at TIMESTAMPTZ NOT NULL,
                    revoked_at TIMESTAMPTZ NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT pk_demo_sessions PRIMARY KEY (id)
                )
            """))
            for owner_id in owner_ids:
                connection.execute(
                    text(
                        f"INSERT INTO {session_table} "
                        "(token_digest, owner_customer_id, idle_expires_at, "
                        "absolute_expires_at) VALUES "
                        "(:digest, :owner_id, :idle, :absolute)"
                    ),
                    {
                        "digest": duplicate_digest,
                        "owner_id": owner_id,
                        "idle": now + timedelta(minutes=30),
                        "absolute": now + timedelta(hours=2),
                    },
                )
        core_before = self._core_signature(engine, schema)

        with self.assertRaises(DemoPersistenceMigrationError) as raised:
            migrate(engine, schema=schema)
        self._assert_safe_migration_error(raised.exception)

        inspector = inspect(engine)
        self.assertEqual(
            {
                table_name
                for table_name in DEMO_TABLES
                if inspector.has_table(table_name, schema=schema)
            },
            {"demo_sessions"},
        )
        self.assertEqual(
            inspector.get_unique_constraints(
                "demo_sessions",
                schema=schema,
            ),
            [],
        )
        self.assertEqual(
            inspector.get_foreign_keys(
                "demo_sessions",
                schema=schema,
            ),
            [],
        )
        self.assertEqual(
            inspector.get_check_constraints(
                "demo_sessions",
                schema=schema,
            ),
            [],
        )
        self.assertEqual(
            inspector.get_indexes(
                "demo_sessions",
                schema=schema,
            ),
            [],
        )
        with engine.connect() as connection:
            self.assertEqual(
                connection.scalar(
                    text(f"SELECT count(*) FROM {session_table}")
                ),
                2,
            )
        self.assertEqual(
            self._core_signature(engine, schema),
            core_before,
        )


if __name__ == "__main__":
    unittest.main()
