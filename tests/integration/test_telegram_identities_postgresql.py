"""Optional PostgreSQL coverage for the additive Telegram identity migration."""

import asyncio
import os
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.agents.orchestrator import AgentOrchestrator
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.identity_service import (
    TelegramIdentityService,
    TelegramIdentityUnavailableError,
)
from app.db.repositories.telegram_identity_repository import TelegramIdentityRepository
from app.integrations.telegram.identity import derive_telegram_user_key
from app.services.authenticated_chat_service import AuthenticatedChatService
from migrations.add_support_tickets import migrate as migrate_support_tickets
from migrations.add_support_ticket_notifications import (
    migrate as migrate_support_ticket_notifications,
)
from migrations.add_conversation_workflow_states import (
    migrate as migrate_workflow_states,
)
from migrations.add_telegram_identities import (
    CUSTOMER_FOREIGN_KEY,
    CUSTOMER_INDEX,
    CUSTOMER_UNIQUE,
    PRIMARY_KEY,
    TelegramIdentityMigrationError,
    USER_KEY_INDEX,
    USER_KEY_UNIQUE,
    migrate,
)


def _skip_reason():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        return "TEST_DATABASE_URL is not configured; Telegram PostgreSQL tests are skipped."
    try:
        candidate = make_url(url)
        primary = make_url(settings.DATABASE_URL)
        if candidate.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if not candidate.database or "test" not in candidate.database.lower():
            return "TEST_DATABASE_URL must name a dedicated database containing 'test'."
        if (candidate.host, candidate.port, candidate.database) == (
            primary.host, primary.port, primary.database
        ):
            return "TEST_DATABASE_URL resolves to the normal application database; refusing to run."
    except Exception:
        return "TEST_DATABASE_URL is invalid; Telegram PostgreSQL tests are skipped."
    return None


SKIP_REASON = _skip_reason()


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestTelegramIdentitiesPostgreSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_engine = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)

    def setUp(self):
        self.schema = f"aura_telegram_test_{uuid4().hex[:12]}"
        self.extra_schemas = []
        with self.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            connection.execute(text(f'''
                CREATE TABLE "{self.schema}".customers (
                    id UUID PRIMARY KEY,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            '''))
            connection.execute(text(f'''
                CREATE TABLE "{self.schema}".reservations (
                    id SERIAL PRIMARY KEY, marker VARCHAR(64) NOT NULL
                )
            '''))
            connection.execute(text(f'''
                CREATE TABLE "{self.schema}".support_tickets (
                    id SERIAL PRIMARY KEY, marker VARCHAR(64) NOT NULL
                )
            '''))
            connection.execute(text(f"INSERT INTO \"{self.schema}\".reservations (marker) VALUES ('keep')"))
            connection.execute(text(f"INSERT INTO \"{self.schema}\".support_tickets (marker) VALUES ('keep')"))
        schema_url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict(
            {"options": f"-csearch_path={self.schema},public"}
        )
        self.engine = create_engine(schema_url, pool_pre_ping=True)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.engine.dispose()
        with self.admin_engine.begin() as connection:
            for schema in self.extra_schemas:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    @classmethod
    def tearDownClass(cls):
        cls.admin_engine.dispose()

    def _create_identity_table(
        self,
        *,
        id_definition="SERIAL NOT NULL",
        key_type="VARCHAR(64)",
        key_nullable="NOT NULL",
        customer_nullable="NOT NULL",
        active_nullable="NOT NULL",
        active_default="TRUE",
        timestamp_default="CURRENT_TIMESTAMP",
        constraints="",
    ):
        with self.engine.begin() as connection:
            connection.execute(text(f'''
                CREATE TABLE "{self.schema}".telegram_identities (
                    id {id_definition},
                    telegram_user_key {key_type} {key_nullable},
                    customer_id UUID {customer_nullable},
                    is_active BOOLEAN {active_nullable} DEFAULT {active_default},
                    created_at TIMESTAMPTZ NOT NULL DEFAULT {timestamp_default},
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT {timestamp_default}
                    {constraints}
                )
            '''))

    @staticmethod
    def _standard_constraints(prefix=","):
        return f'''
            {prefix} CONSTRAINT "{PRIMARY_KEY}" PRIMARY KEY (id),
            CONSTRAINT "{USER_KEY_UNIQUE}" UNIQUE (telegram_user_key),
            CONSTRAINT "{CUSTOMER_UNIQUE}" UNIQUE (customer_id),
            CONSTRAINT "{CUSTOMER_FOREIGN_KEY}" FOREIGN KEY (customer_id) REFERENCES customers(id)
        '''

    def test_migration_is_additive_and_idempotent(self):
        marker_customer = uuid4()
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO customers (id, token_version, is_active) VALUES (:id, 1, TRUE)"
            ), {"id": marker_customer})
        with self.engine.connect() as connection:
            customers_before = connection.execute(text("SELECT id, token_version, is_active FROM customers ORDER BY id")).all()
            reservations_before = connection.execute(text("SELECT marker FROM reservations")).all()
            tickets_before = connection.execute(text("SELECT marker FROM support_tickets")).all()
        self.assertTrue(migrate(self.engine, schema=self.schema))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        with self.engine.begin() as connection:
            connection.execute(text(
                f'DROP INDEX "{self.schema}"."ix_telegram_identities_user_key"'
            ))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        inspector = inspect(self.engine)
        columns = {item["name"] for item in inspector.get_columns("telegram_identities", schema=self.schema)}
        unique = {item["name"] for item in inspector.get_unique_constraints("telegram_identities", schema=self.schema)}
        indexes = {item["name"] for item in inspector.get_indexes("telegram_identities", schema=self.schema)}
        self.assertEqual(columns, {"id", "telegram_user_key", "customer_id", "is_active", "created_at", "updated_at"})
        self.assertIn("uq_telegram_identities_user_key", unique)
        self.assertIn("uq_telegram_identities_customer_id", unique)
        self.assertIn("ix_telegram_identities_user_key", indexes)
        self.assertIn("ix_telegram_identities_customer_id", indexes)
        foreign_keys = inspect(self.engine).get_foreign_keys("telegram_identities", schema=self.schema)
        self.assertTrue(any(
            item["name"] == CUSTOMER_FOREIGN_KEY
            and item["referred_schema"] == self.schema
            and item["referred_table"] == "customers"
            for item in foreign_keys
        ))
        with self.engine.connect() as connection:
            self.assertEqual(customers_before, connection.execute(text("SELECT id, token_version, is_active FROM customers ORDER BY id")).all())
            self.assertEqual(reservations_before, connection.execute(text("SELECT marker FROM reservations")).all())
            self.assertEqual(tickets_before, connection.execute(text("SELECT marker FROM support_tickets")).all())

    def test_identity_reuses_same_customer_and_hides_raw_id(self):
        migrate(self.engine, schema=self.schema)
        service = TelegramIdentityService()
        first = self.SessionLocal()
        try:
            customer_a = service.resolve_or_create(first, telegram_user_id=1001, identity_secret="x" * 32)
            first_id = customer_a.id
        finally:
            first.close()
        second = self.SessionLocal()
        try:
            self.assertEqual(
                service.resolve_or_create(second, telegram_user_id=1001, identity_secret="x" * 32).id,
                first_id,
            )
            self.assertNotEqual(
                service.resolve_or_create(second, telegram_user_id=1002, identity_secret="x" * 32).id,
                first_id,
            )
        finally:
            second.close()
        with self.engine.connect() as connection:
            values = connection.execute(text("SELECT telegram_user_key FROM telegram_identities")).scalars().all()
        self.assertEqual(set(values), {
            derive_telegram_user_key("x" * 32, 1001),
            derive_telegram_user_key("x" * 32, 1002),
        })
        columns = {item["name"] for item in inspect(self.engine).get_columns("telegram_identities", schema=self.schema)}
        self.assertNotIn("telegram_user_id", columns)
        self.assertNotIn("telegram_chat_id", columns)

    def test_missing_lookup_indexes_are_restored(self):
        migrate(self.engine, schema=self.schema)
        with self.engine.begin() as connection:
            connection.execute(text(f'DROP INDEX "{self.schema}"."{USER_KEY_INDEX}"'))
            connection.execute(text(f'DROP INDEX "{self.schema}"."{CUSTOMER_INDEX}"'))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        indexes = {item["name"] for item in inspect(self.engine).get_indexes("telegram_identities", schema=self.schema)}
        self.assertTrue({USER_KEY_INDEX, CUSTOMER_INDEX}.issubset(indexes))

    def test_missing_unique_constraint_is_restored(self):
        migrate(self.engine, schema=self.schema)
        with self.engine.begin() as connection:
            connection.execute(text(f'ALTER TABLE "{self.schema}".telegram_identities DROP CONSTRAINT "{USER_KEY_UNIQUE}"'))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        names = {item["name"] for item in inspect(self.engine).get_unique_constraints("telegram_identities", schema=self.schema)}
        self.assertIn(USER_KEY_UNIQUE, names)

    def test_semantic_constraints_are_renamed_to_stable_names(self):
        self._create_identity_table(constraints=''',
            CONSTRAINT "legacy_pk" PRIMARY KEY (id),
            CONSTRAINT "legacy_user_unique" UNIQUE (telegram_user_key),
            CONSTRAINT "legacy_customer_unique" UNIQUE (customer_id),
            CONSTRAINT "legacy_customer_fk" FOREIGN KEY (customer_id) REFERENCES customers(id)
        ''')
        self.assertTrue(migrate(self.engine, schema=self.schema))
        inspector = inspect(self.engine)
        self.assertEqual(inspector.get_pk_constraint("telegram_identities", schema=self.schema)["name"], PRIMARY_KEY)
        unique_names = {item["name"] for item in inspector.get_unique_constraints("telegram_identities", schema=self.schema)}
        self.assertTrue({USER_KEY_UNIQUE, CUSTOMER_UNIQUE}.issubset(unique_names))
        self.assertIn(CUSTOMER_FOREIGN_KEY, {item["name"] for item in inspector.get_foreign_keys("telegram_identities", schema=self.schema)})

    def test_primary_key_without_generator_fails_closed(self):
        self._create_identity_table(id_definition="INTEGER NOT NULL", constraints=self._standard_constraints())
        with self.assertRaises(TelegramIdentityMigrationError):
            migrate(self.engine, schema=self.schema)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT 1")).scalar_one(), 1)

    def test_malformed_columns_fail_closed(self):
        cases = (
            {"key_type": "VARCHAR(63)"},
            {"key_nullable": "NULL"},
            {"customer_nullable": "NULL"},
            {"active_nullable": "NULL"},
        )
        for index, kwargs in enumerate(cases):
            if index:
                with self.engine.begin() as connection:
                    connection.execute(text(f'DROP TABLE "{self.schema}".telegram_identities'))
            self._create_identity_table(constraints=self._standard_constraints(), **kwargs)
            with self.subTest(case=kwargs), self.assertRaises(TelegramIdentityMigrationError):
                migrate(self.engine, schema=self.schema)

    def test_malformed_defaults_fail_closed(self):
        cases = (
            {"active_default": "FALSE"},
            {"timestamp_default": "'2000-01-01T00:00:00Z'::timestamptz"},
        )
        for index, kwargs in enumerate(cases):
            if index:
                with self.engine.begin() as connection:
                    connection.execute(text(f'DROP TABLE "{self.schema}".telegram_identities'))
            self._create_identity_table(constraints=self._standard_constraints(), **kwargs)
            with self.subTest(case=kwargs), self.assertRaises(TelegramIdentityMigrationError):
                migrate(self.engine, schema=self.schema)

    def test_wrong_named_unique_definition_fails_closed(self):
        self._create_identity_table(constraints=f''',
            CONSTRAINT "{PRIMARY_KEY}" PRIMARY KEY (id),
            CONSTRAINT "{USER_KEY_UNIQUE}" UNIQUE (telegram_user_key, customer_id),
            CONSTRAINT "{CUSTOMER_UNIQUE}" UNIQUE (customer_id),
            CONSTRAINT "{CUSTOMER_FOREIGN_KEY}" FOREIGN KEY (customer_id) REFERENCES customers(id)
        ''')
        with self.assertRaises(TelegramIdentityMigrationError):
            migrate(self.engine, schema=self.schema)

    def test_wrong_foreign_key_target_and_schema_fail_closed(self):
        other_schema = f"{self.schema}_other"
        self.extra_schemas.append(other_schema)
        with self.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{other_schema}"'))
            connection.execute(text(f'CREATE TABLE "{other_schema}".customers (id UUID PRIMARY KEY)'))
        self._create_identity_table(constraints=f''',
            CONSTRAINT "{PRIMARY_KEY}" PRIMARY KEY (id),
            CONSTRAINT "{USER_KEY_UNIQUE}" UNIQUE (telegram_user_key),
            CONSTRAINT "{CUSTOMER_UNIQUE}" UNIQUE (customer_id),
            CONSTRAINT "{CUSTOMER_FOREIGN_KEY}" FOREIGN KEY (customer_id) REFERENCES "{other_schema}".customers(id)
        ''')
        with self.assertRaises(TelegramIdentityMigrationError):
            migrate(self.engine, schema=self.schema)

    def test_wrong_foreign_key_table_fails_closed(self):
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE other_customers (id UUID PRIMARY KEY)"))
        self._create_identity_table(constraints=f''',
            CONSTRAINT "{PRIMARY_KEY}" PRIMARY KEY (id),
            CONSTRAINT "{USER_KEY_UNIQUE}" UNIQUE (telegram_user_key),
            CONSTRAINT "{CUSTOMER_UNIQUE}" UNIQUE (customer_id),
            CONSTRAINT "{CUSTOMER_FOREIGN_KEY}" FOREIGN KEY (customer_id) REFERENCES other_customers(id)
        ''')
        with self.assertRaises(TelegramIdentityMigrationError):
            migrate(self.engine, schema=self.schema)

    def test_schema_collision_does_not_redirect_migration(self):
        other_schema = f"{self.schema}_collision"
        self.extra_schemas.append(other_schema)
        with self.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{other_schema}"'))
            connection.execute(text(f'CREATE TABLE "{other_schema}".telegram_identities (id TEXT PRIMARY KEY)'))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        columns = inspect(self.engine).get_columns("telegram_identities", schema=self.schema)
        self.assertEqual(columns[0]["name"], "id")
        with self.admin_engine.connect() as connection:
            other_type = connection.execute(text(f'''
                SELECT data_type FROM information_schema.columns
                WHERE table_schema=:schema AND table_name='telegram_identities' AND column_name='id'
            '''), {"schema": other_schema}).scalar_one()
        self.assertEqual(other_type, "text")

    def test_failure_rolls_back_prior_constraint_renames(self):
        self._create_identity_table(constraints=''',
            CONSTRAINT "legacy_pk" PRIMARY KEY (id),
            CONSTRAINT "legacy_user_unique" UNIQUE (telegram_user_key),
            CONSTRAINT "legacy_customer_unique" UNIQUE (customer_id),
            CONSTRAINT "legacy_customer_fk" FOREIGN KEY (customer_id) REFERENCES customers(id)
        ''')
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE INDEX "{USER_KEY_INDEX}" ON "{self.schema}".telegram_identities (customer_id)'))
        with self.assertRaises(TelegramIdentityMigrationError):
            migrate(self.engine, schema=self.schema)
        inspector = inspect(self.engine)
        self.assertEqual(inspector.get_pk_constraint("telegram_identities", schema=self.schema)["name"], "legacy_pk")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT 1")).scalar_one(), 1)

    def test_concurrent_same_user_converges_without_orphan_customer(self):
        migrate(self.engine, schema=self.schema)
        barrier = threading.Barrier(2)
        local = threading.local()

        class BarrierRepository(TelegramIdentityRepository):
            def get_by_user_key(inner_self, db, telegram_user_key):
                result = super(BarrierRepository, inner_self).get_by_user_key(db, telegram_user_key)
                if result is None and not getattr(local, "waited", False):
                    local.waited = True
                    barrier.wait(timeout=10)
                return result

        repository = BarrierRepository()
        secret = "concurrent-telegram-identity-secret"

        def resolve():
            db = self.SessionLocal()
            try:
                customer = TelegramIdentityService(repository).resolve_or_create(
                    db,
                    telegram_user_id=777001,
                    identity_secret=secret,
                )
                self.assertEqual(db.execute(text("SELECT 1")).scalar_one(), 1)
                return customer.id
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            customer_ids = list(executor.map(lambda _: resolve(), range(2)))
        self.assertEqual(customer_ids[0], customer_ids[1])
        expected_key = derive_telegram_user_key(secret, 777001)
        with self.engine.connect() as connection:
            identity_rows = connection.execute(text(
                "SELECT telegram_user_key, customer_id FROM telegram_identities"
            )).all()
            customer_count = connection.execute(text("SELECT count(*) FROM customers")).scalar_one()
        self.assertEqual(identity_rows, [(expected_key, customer_ids[0])])
        self.assertEqual(customer_count, 1)

    def test_concurrent_different_users_get_different_customers(self):
        migrate(self.engine, schema=self.schema)
        barrier = threading.Barrier(2)
        local = threading.local()

        class BarrierRepository(TelegramIdentityRepository):
            def get_by_user_key(inner_self, db, telegram_user_key):
                result = super(BarrierRepository, inner_self).get_by_user_key(db, telegram_user_key)
                if result is None and not getattr(local, "waited", False):
                    local.waited = True
                    barrier.wait(timeout=10)
                return result

        def resolve(user_id):
            db = self.SessionLocal()
            try:
                return TelegramIdentityService(BarrierRepository()).resolve_or_create(
                    db, telegram_user_id=user_id, identity_secret="x" * 32
                ).id
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            customer_ids = list(executor.map(resolve, (777101, 777102)))
        self.assertNotEqual(customer_ids[0], customer_ids[1])

    def test_inactive_persisted_identity_fails_closed_and_session_remains_usable(self):
        migrate(self.engine, schema=self.schema)
        first = self.SessionLocal()
        try:
            TelegramIdentityService().resolve_or_create(
                first, telegram_user_id=777201, identity_secret="x" * 32
            )
        finally:
            first.close()
        with self.engine.begin() as connection:
            connection.execute(text(
                "UPDATE telegram_identities SET is_active=FALSE WHERE telegram_user_key=:key"
            ), {"key": derive_telegram_user_key("x" * 32, 777201)})
        second = self.SessionLocal()
        try:
            with self.assertRaises(TelegramIdentityUnavailableError):
                TelegramIdentityService().resolve_or_create(
                    second, telegram_user_id=777201, identity_secret="x" * 32
                )
            self.assertEqual(second.execute(text("SELECT 1")).scalar_one(), 1)
        finally:
            second.close()

    def test_handoff_ticket_lock_isolation_and_restart_recovery_through_handler(self):
        with self.engine.begin() as connection:
            connection.execute(text(f'DROP TABLE "{self.schema}".support_tickets'))
        migrate_support_tickets(self.engine, schema=self.schema)
        migrate_support_ticket_notifications(self.engine, schema=self.schema)
        migrate_workflow_states(self.engine, schema=self.schema)
        migrate(self.engine, schema=self.schema)

        class Message:
            def __init__(inner_self, value):
                inner_self.text = value
                inner_self.replies = []

            async def reply_text(inner_self, value, **kwargs):
                inner_self.replies.append(value)

        def update_for(user_id, message):
            return SimpleNamespace(
                effective_user=SimpleNamespace(id=user_id),
                effective_chat=SimpleNamespace(id=user_id, type="private"),
                effective_message=Message(message),
            )

        def complete_reply(update):
            chunks = update.effective_message.replies
            self.assertTrue(chunks)
            self.assertTrue(all(isinstance(chunk, str) for chunk in chunks))
            self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
            return "".join(chunks)

        secret = "handler-handoff-identity-secret-v1"
        first_agent = AgentOrchestrator()
        first_agent.update_reservation_agent.run = AsyncMock(side_effect=AssertionError("Update must remain locked"))
        first_agent.cancel_reservation_agent.run = AsyncMock(side_effect=AssertionError("Cancel must remain locked"))
        first_handlers = TelegramCustomerHandlers(
            identity_secret=secret,
            session_factory=self.SessionLocal,
            chat_service=AuthenticatedChatService(agent=first_agent),
        )

        first = update_for(880001, "hubungkan saya ke Rizal")
        asyncio.run(first_handlers.text_message(first, None))
        first_reply = complete_reply(first)
        ticket_match = re.search(r"Nomor tiket Anda: (CS-[0-9]{4}-[0-9]{6,})", first_reply)
        self.assertIsNotNone(ticket_match)
        ticket_number = ticket_match.group(1)
        self.assertNotIn("notifikasi", first_reply.lower())

        for locked_message in ("ubah reservasi saya", "batalkan reservasi saya"):
            locked = update_for(880001, locked_message)
            asyncio.run(first_handlers.text_message(locked, None))
            locked_reply = complete_reply(locked)
            self.assertIn(ticket_number, locked_reply)
            self.assertNotIn("notifikasi", locked_reply.lower())
        first_agent.update_reservation_agent.run.assert_not_awaited()
        first_agent.cancel_reservation_agent.run.assert_not_awaited()

        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM support_tickets")).scalar_one(), 1)
            self.assertEqual(
                connection.execute(
                    text("SELECT count(*) FROM support_ticket_notifications")
                ).scalar_one(),
                1,
            )
            first_customer = connection.execute(text(
                "SELECT customer_id FROM telegram_identities WHERE telegram_user_key=:key"
            ), {"key": derive_telegram_user_key(secret, 880001)}).scalar_one()

        # A new agent/handler simulates cleared in-process memory after restart.
        restarted_agent = AgentOrchestrator()
        restarted_handlers = TelegramCustomerHandlers(
            identity_secret=secret,
            session_factory=self.SessionLocal,
            chat_service=AuthenticatedChatService(agent=restarted_agent),
        )
        after_restart = update_for(880001, "halo lagi")
        asyncio.run(restarted_handlers.text_message(after_restart, None))
        self.assertIn(ticket_number, complete_reply(after_restart))

        other_status = update_for(880002, "/status")
        asyncio.run(restarted_handlers.status(other_status, None))
        self.assertIn("tidak memiliki tiket", other_status.effective_message.replies[0])
        self.assertNotIn(ticket_number, other_status.effective_message.replies[0])

        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM support_tickets")).scalar_one(), 1)
            self.assertEqual(
                connection.execute(
                    text("SELECT count(*) FROM support_ticket_notifications")
                ).scalar_one(),
                1,
            )
            self.assertEqual(connection.execute(text(
                "SELECT customer_id FROM telegram_identities WHERE telegram_user_key=:key"
            ), {"key": derive_telegram_user_key(secret, 880001)}).scalar_one(), first_customer)
            self.assertEqual(connection.execute(text("SELECT count(*) FROM telegram_identities")).scalar_one(), 2)
