"""Opt-in PostgreSQL coverage for the Phase E transactional outbox."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.transaction_errors import PersistenceOperationError
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.services.handoff.notification_outbox_service import NotificationOutboxService
from app.services.handoff.ticket_service import TicketService
from migrations.add_support_tickets import migrate as migrate_tickets
from migrations.add_support_ticket_notifications import (
    CHANNEL_CHECK,
    DUE_INDEX,
    LEASE_INDEX,
    STATUS_CHECK,
    UNIQUE_TICKET_CHANNEL,
    SupportTicketNotificationMigrationError,
    migrate,
)
from tests.integration.disposable_schema import (
    DisposableSchemaCleanupError,
    DisposableSchemaResources,
)


def _identity(url):
    parsed = make_url(url)
    return parsed.get_backend_name(), parsed.host, parsed.port, parsed.database


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return "TEST_DATABASE_URL is not configured; owner-notification PostgreSQL tests are skipped."
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if not parsed.database or "test" not in parsed.database.lower():
            return "TEST_DATABASE_URL must name a dedicated database containing 'test'."
        if _identity(value) == _identity(settings.DATABASE_URL):
            return "TEST_DATABASE_URL resolves to the normal database; refusing to run."
    except Exception:
        return "TEST_DATABASE_URL is invalid; owner-notification PostgreSQL tests are skipped."
    return None


SKIP_REASON = _skip_reason()


class TestDisposableSchemaResources(unittest.TestCase):
    @staticmethod
    def _resources(schema="aura_owner_notification_test_0123456789ab"):
        admin = MagicMock()
        connection = admin.begin.return_value.__enter__.return_value
        resources = DisposableSchemaResources(
            admin_engine=admin,
            schema=schema,
            allowed_prefixes=("aura_owner_notification_test_",),
            dispose_admin=False,
        )
        return resources, admin, connection

    def test_registered_cleanup_runs_after_simulated_setup_failure(self):
        resources, _admin, connection = self._resources()

        class FailingSetup(unittest.TestCase):
            def setUp(inner_self):
                inner_self.addCleanup(resources.cleanup)
                raise RuntimeError("controlled setup failure")

            def runTest(inner_self):
                inner_self.fail("setup failure must prevent the test body")

        result = unittest.TestResult()
        FailingSetup().run(result)

        self.assertEqual(len(result.errors), 1)
        self.assertIn("controlled setup failure", result.errors[0][1])
        statement = str(connection.execute.call_args.args[0])
        self.assertEqual(
            statement,
            'DROP SCHEMA IF EXISTS "aura_owner_notification_test_0123456789ab" CASCADE',
        )

    def test_registered_class_cleanup_runs_after_set_up_class_failure(self):
        resources, _admin, connection = self._resources()

        class FailingClassSetup(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.addClassCleanup(resources.cleanup)
                raise RuntimeError("controlled class setup failure")

            def test_never_runs(self):
                self.fail("class setup failure must prevent the test body")

        result = unittest.TestResult()
        unittest.TestLoader().loadTestsFromTestCase(FailingClassSetup).run(result)

        self.assertEqual(len(result.errors), 1)
        self.assertIn("controlled class setup failure", result.errors[0][1])
        statement = str(connection.execute.call_args.args[0])
        self.assertEqual(
            statement,
            'DROP SCHEMA IF EXISTS "aura_owner_notification_test_0123456789ab" CASCADE',
        )

    def test_cleanup_is_idempotent_and_disposes_tracked_engine(self):
        resources, _admin, connection = self._resources()
        scoped_engine = MagicMock()
        resources.track_engine(scoped_engine)

        resources.cleanup()
        resources.cleanup()

        self.assertEqual(scoped_engine.dispose.call_count, 2)
        self.assertEqual(connection.execute.call_count, 2)

    def test_cleanup_rejects_public_and_unrelated_schemas(self):
        for unsafe_schema in ("public", "customer_supplied", "aura_other_0123456789"):
            with self.subTest(schema=unsafe_schema):
                resources, admin, connection = self._resources(unsafe_schema)
                with self.assertRaises(DisposableSchemaCleanupError):
                    resources.cleanup()
                admin.begin.assert_not_called()
                connection.execute.assert_not_called()

    def test_cleanup_failure_does_not_replace_setup_failure(self):
        resources, admin, _connection = self._resources()
        admin.begin.side_effect = RuntimeError("controlled cleanup failure")

        class FailingSetupAndCleanup(unittest.TestCase):
            def setUp(inner_self):
                inner_self.addCleanup(resources.cleanup)
                raise RuntimeError("controlled original setup failure")

            def runTest(inner_self):
                inner_self.fail("setup failure must prevent the test body")

        result = unittest.TestResult()
        FailingSetupAndCleanup().run(result)
        rendered_errors = "\n".join(error for _test, error in result.errors)

        self.assertEqual(len(result.errors), 2)
        self.assertIn("controlled original setup failure", rendered_errors)
        self.assertIn("DisposableSchemaCleanupError", rendered_errors)
        self.assertNotIn("controlled cleanup failure", rendered_errors)


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestOwnerNotificationsPostgreSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
        cls.addClassCleanup(cls.admin.dispose)

    def setUp(self):
        self.schema = f"aura_owner_notification_test_{uuid4().hex[:12]}"
        self.schema_resources = DisposableSchemaResources(
            admin_engine=self.admin,
            schema=self.schema,
            allowed_prefixes=(
                "aura_owner_notification_test_",
                "aura_owner_wrong_fk_",
            ),
            dispose_admin=False,
        )
        self.addCleanup(self.schema_resources.cleanup)
        with self.admin.begin() as connection:
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
                    id SERIAL PRIMARY KEY, marker VARCHAR(32) NOT NULL
                )
            '''))
            connection.execute(text(f'''
                CREATE TABLE "{self.schema}".telegram_identities (
                    id SERIAL PRIMARY KEY, marker VARCHAR(32) NOT NULL
                )
            '''))
            connection.execute(text(f"INSERT INTO \"{self.schema}\".reservations (marker) VALUES ('keep')"))
            connection.execute(text(f"INSERT INTO \"{self.schema}\".telegram_identities (marker) VALUES ('keep')"))
        url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict(
            {"options": f"-csearch_path={self.schema},public"}
        )
        self.engine = create_engine(url, pool_pre_ping=True)
        self.schema_resources.track_engine(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        migrate_tickets(self.engine, schema=self.schema)

    def table(self, name):
        return f'"{self.schema}"."{name}"'

    def insert_customer(self):
        owner = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(f"INSERT INTO {self.table('customers')} (id) VALUES (:id)"),
                {"id": owner},
            )
        return owner

    def create_notification_table(
        self,
        *,
        id_definition="SERIAL NOT NULL",
        status_type="VARCHAR(16)",
        foreign_target=None,
        extra_constraints="",
    ):
        foreign_target = foreign_target or f"{self.table('support_tickets')} (id)"
        with self.engine.begin() as connection:
            connection.execute(text(f'''
                CREATE TABLE {self.table('support_ticket_notifications')} (
                    id {id_definition}, support_ticket_id INTEGER NOT NULL,
                    channel VARCHAR(32) NOT NULL DEFAULT 'telegram_owner',
                    status {status_type} NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    lease_expires_at TIMESTAMPTZ NULL, sent_at TIMESTAMPTZ NULL,
                    telegram_message_id BIGINT NULL, last_error_code VARCHAR(32) NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT legacy_notification_pk PRIMARY KEY (id),
                    CONSTRAINT legacy_notification_fk FOREIGN KEY (support_ticket_id)
                        REFERENCES {foreign_target}
                    {extra_constraints}
                )
            '''))

    @staticmethod
    def state():
        return {
            "category": "explicit_human_request",
            "reason_code": "explicit_human_request",
            "priority": "high",
            "attempt_count": 1,
        }

    def test_migration_fresh_repeated_converged_and_no_backfill_or_other_table_changes(self):
        owner = self.insert_customer()
        with self.engine.begin() as connection:
            connection.execute(text(f'''
                INSERT INTO {self.table('support_tickets')} (
                    ticket_number, owner_customer_id, session_reference_hash,
                    category, reason_code, priority, safe_summary, status, attempt_count
                ) VALUES (
                    'CS-2026-999999', :owner, :hash,
                    'explicit_human_request', 'explicit_human_request', 'high',
                    'Customer requested human assistance.', 'open', 1
                )
            '''), {"owner": owner, "hash": "f" * 64})
        self.assertTrue(migrate(self.engine, schema=self.schema))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        inspector = inspect(self.engine)
        indexes = {item["name"] for item in inspector.get_indexes("support_ticket_notifications", schema=self.schema)}
        checks = {item["name"] for item in inspector.get_check_constraints("support_ticket_notifications", schema=self.schema)}
        uniques = {item["name"] for item in inspector.get_unique_constraints("support_ticket_notifications", schema=self.schema)}
        self.assertIn(DUE_INDEX, indexes)
        self.assertIn(LEASE_INDEX, indexes)
        self.assertIn(CHANNEL_CHECK, checks)
        self.assertIn(STATUS_CHECK, checks)
        self.assertIn(UNIQUE_TICKET_CHANNEL, uniques)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text(f"SELECT COUNT(*) FROM {self.table('support_ticket_notifications')}" )).scalar_one(), 0)
            self.assertEqual(connection.execute(text(f"SELECT COUNT(*) FROM {self.table('support_tickets')}" )).scalar_one(), 1)
            self.assertEqual(connection.execute(text(f"SELECT COUNT(*) FROM {self.table('customers')}" )).scalar_one(), 1)
            self.assertEqual(connection.execute(text(f"SELECT marker FROM {self.table('reservations')}" )).scalar_one(), "keep")
            self.assertEqual(connection.execute(text(f"SELECT marker FROM {self.table('telegram_identities')}" )).scalar_one(), "keep")

    def test_migration_restores_missing_safe_index_and_check(self):
        migrate(self.engine, schema=self.schema)
        with self.engine.begin() as connection:
            connection.execute(text(f'ALTER TABLE {self.table("support_ticket_notifications")} DROP CONSTRAINT "{CHANNEL_CHECK}"'))
            connection.execute(text(f'DROP INDEX "{self.schema}"."{DUE_INDEX}"'))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        inspector = inspect(self.engine)
        self.assertIn(CHANNEL_CHECK, {item["name"] for item in inspector.get_check_constraints("support_ticket_notifications", schema=self.schema)})
        self.assertIn(DUE_INDEX, {item["name"] for item in inspector.get_indexes("support_ticket_notifications", schema=self.schema)})

    def test_migration_restores_missing_named_unique_constraint(self):
        migrate(self.engine, schema=self.schema)
        with self.engine.begin() as connection:
            connection.execute(text(
                f'ALTER TABLE {self.table("support_ticket_notifications")} '
                f'DROP CONSTRAINT "{UNIQUE_TICKET_CHANNEL}"'
            ))
        self.assertTrue(migrate(self.engine, schema=self.schema))
        inspector = inspect(self.engine)
        self.assertIn(
            UNIQUE_TICKET_CHANNEL,
            {item["name"] for item in inspector.get_unique_constraints("support_ticket_notifications", schema=self.schema)},
        )

    def test_malformed_column_and_primary_key_without_generator_fail_closed(self):
        self.create_notification_table(status_type="VARCHAR(99)")
        with self.assertRaises(SupportTicketNotificationMigrationError):
            migrate(self.engine, schema=self.schema)
        with self.engine.begin() as connection:
            connection.execute(text(f"DROP TABLE {self.table('support_ticket_notifications')}"))
        self.create_notification_table(id_definition="INTEGER NOT NULL")
        with self.assertRaises(SupportTicketNotificationMigrationError):
            migrate(self.engine, schema=self.schema)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT 1")).scalar_one(), 1)

    def test_wrong_foreign_key_schema_fails_closed(self):
        other = f"aura_owner_wrong_fk_{uuid4().hex[:10]}"
        self.schema_resources.track_schema(other)
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{other}"'))
            connection.execute(text(f'CREATE TABLE "{other}".support_tickets (id SERIAL PRIMARY KEY)'))
        self.create_notification_table(
            foreign_target=f'"{other}".support_tickets (id)'
        )
        with self.assertRaises(SupportTicketNotificationMigrationError):
            migrate(self.engine, schema=self.schema)
        foreign_key = inspect(self.engine).get_foreign_keys(
            "support_ticket_notifications", schema=self.schema
        )[0]
        self.assertEqual(foreign_key["referred_schema"], other)

    def test_partial_constraint_convergence_rolls_back_on_later_incompatibility(self):
        self.create_notification_table(
            extra_constraints=(
                f", CONSTRAINT \"{STATUS_CHECK}\" CHECK (status IN ('pending'))"
            )
        )
        with self.assertRaises(SupportTicketNotificationMigrationError):
            migrate(self.engine, schema=self.schema)
        primary_key = inspect(self.engine).get_pk_constraint(
            "support_ticket_notifications", schema=self.schema
        )
        self.assertEqual(primary_key["name"], "legacy_notification_pk")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT 1")).scalar_one(), 1)

    def test_atomic_ticket_outbox_creation_reuse_and_forced_failure(self):
        migrate(self.engine, schema=self.schema)
        owner = self.insert_customer()
        db = self.Session()
        try:
            service = TicketService()
            ticket = service.create_or_get(db, owner_customer_id=owner, memory_key="owner:one", handoff_state=self.state())
            reused = service.create_or_get(db, owner_customer_id=owner, memory_key="owner:one", handoff_state=self.state())
            self.assertEqual(ticket.id, reused.id)
        finally:
            db.close()
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text(f"SELECT COUNT(*) FROM {self.table('support_ticket_notifications')}" )).scalar_one(), 1)

        class FailingOutbox(NotificationOutboxService):
            def _stage_new_ticket(self, db, *, ticket):
                raise RuntimeError("synthetic")

        db = self.Session()
        try:
            with self.assertRaises(PersistenceOperationError):
                TicketService(SupportTicketRepository(), FailingOutbox()).create_or_get(
                    db, owner_customer_id=owner, memory_key="owner:failure", handoff_state=self.state()
                )
            self.assertEqual(db.execute(text("SELECT 1")).scalar_one(), 1)
        finally:
            db.close()
        with self.engine.connect() as connection:
            session_hash = TicketService.hash_session_reference("owner:failure")
            self.assertEqual(connection.execute(text(f"SELECT COUNT(*) FROM {self.table('support_tickets')} WHERE session_reference_hash=:hash"), {"hash": session_hash}).scalar_one(), 0)
            self.assertEqual(connection.execute(text(
                f"SELECT COUNT(*) FROM {self.table('support_ticket_notifications')} "
                f"WHERE support_ticket_id IN ("
                f"SELECT id FROM {self.table('support_tickets')} "
                f"WHERE session_reference_hash=:hash)"
            ), {"hash": session_hash}).scalar_one(), 0)

        db = self.Session()
        try:
            recovered = TicketService().create_or_get(
                db,
                owner_customer_id=owner,
                memory_key="owner:failure",
                handoff_state=self.state(),
            )
            self.assertTrue(recovered.ticket_number.startswith("CS-"))
        finally:
            db.close()
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text(
                f"SELECT COUNT(*) FROM {self.table('support_tickets')} "
                f"WHERE session_reference_hash=:hash"
            ), {"hash": session_hash}).scalar_one(), 1)
            self.assertEqual(connection.execute(text(
                f"SELECT COUNT(*) FROM {self.table('support_ticket_notifications')} "
                f"WHERE support_ticket_id IN ("
                f"SELECT id FROM {self.table('support_tickets')} "
                f"WHERE session_reference_hash=:hash)"
            ), {"hash": session_hash}).scalar_one(), 1)

    def test_different_customers_receive_separate_ticket_and_outbox_rows(self):
        migrate(self.engine, schema=self.schema)
        owner_a = self.insert_customer()
        owner_b = self.insert_customer()
        for owner in (owner_a, owner_b):
            db = self.Session()
            try:
                TicketService().create_or_get(
                    db, owner_customer_id=owner, memory_key=f"{owner}:same-session",
                    handoff_state=self.state(),
                )
            finally:
                db.close()
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text(f"SELECT COUNT(*) FROM {self.table('support_tickets')}" )).scalar_one(), 2)
            self.assertEqual(connection.execute(text(f"SELECT COUNT(*) FROM {self.table('support_ticket_notifications')}" )).scalar_one(), 2)

    def test_two_claimers_converge_and_expired_lease_recovers(self):
        migrate(self.engine, schema=self.schema)
        owner = self.insert_customer()
        db = self.Session()
        try:
            TicketService().create_or_get(db, owner_customer_id=owner, memory_key="owner:claim", handoff_state=self.state())
        finally:
            db.close()
        def concurrent_claim():
            barrier = threading.Barrier(2)

            def claim():
                session = self.Session()
                try:
                    barrier.wait(timeout=10)
                    row = NotificationOutboxService().claim_due(
                        session,
                        lease_seconds=60,
                    )
                    usable = session.execute(text("SELECT 1")).scalar_one()
                    return None if row is None else row.id, usable
                finally:
                    session.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                return list(executor.map(lambda _value: claim(), range(2)))

        claimed = concurrent_claim()
        claimed_ids = [item[0] for item in claimed]
        self.assertEqual(sum(item is not None for item in claimed_ids), 1)
        self.assertEqual([item[1] for item in claimed], [1, 1])
        notification_id = next(item for item in claimed_ids if item is not None)
        with self.engine.connect() as connection:
            status, lease = connection.execute(text(
                f"SELECT status, lease_expires_at "
                f"FROM {self.table('support_ticket_notifications')} "
                f"WHERE id=:id"
            ), {"id": notification_id}).one()
        self.assertEqual(status, "sending")
        self.assertIsNotNone(lease)

        # A committed, non-expired lease cannot be reclaimed.
        session = self.Session()
        try:
            self.assertIsNone(
                NotificationOutboxService().claim_due(session, lease_seconds=60)
            )
        finally:
            session.close()

        with self.engine.begin() as connection:
            connection.execute(text(
                f"UPDATE {self.table('support_ticket_notifications')} "
                f"SET lease_expires_at=:expired WHERE id=:id"
            ), {
                "expired": datetime.now(timezone.utc) - timedelta(seconds=1),
                "id": notification_id,
            })

        recovered = concurrent_claim()
        recovered_ids = [item[0] for item in recovered]
        self.assertEqual(sum(item is not None for item in recovered_ids), 1)
        self.assertEqual(
            next(item for item in recovered_ids if item is not None),
            notification_id,
        )

        session = self.Session()
        try:
            NotificationOutboxService().mark_sent(
                session,
                notification_id=notification_id,
                telegram_message_id=1,
            )
            self.assertIsNone(
                NotificationOutboxService().claim_due(session, lease_seconds=60)
            )
        finally:
            session.close()

        # Repeat the two-Session race to catch scheduling-dependent regressions.
        for iteration in range(10):
            db = self.Session()
            try:
                TicketService().create_or_get(
                    db,
                    owner_customer_id=owner,
                    memory_key=f"owner:claim-race:{iteration}",
                    handoff_state=self.state(),
                )
            finally:
                db.close()
            raced = concurrent_claim()
            raced_ids = [item[0] for item in raced]
            self.assertEqual(
                sum(item is not None for item in raced_ids),
                1,
                f"iteration {iteration}",
            )
            winner = next(item for item in raced_ids if item is not None)
            session = self.Session()
            try:
                NotificationOutboxService().mark_sent(
                    session,
                    notification_id=winner,
                    telegram_message_id=iteration + 2,
                )
            finally:
                session.close()

    def test_incompatible_foreign_key_rolls_back_and_connection_remains_usable(self):
        with self.engine.begin() as connection:
            connection.execute(text(f'''
                CREATE TABLE {self.table('support_ticket_notifications')} (
                    id SERIAL NOT NULL,
                    support_ticket_id INTEGER NOT NULL REFERENCES {self.table('reservations')} (id),
                    channel VARCHAR(32) NOT NULL DEFAULT 'telegram_owner',
                    status VARCHAR(16) NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    lease_expires_at TIMESTAMPTZ NULL, sent_at TIMESTAMPTZ NULL,
                    telegram_message_id BIGINT NULL, last_error_code VARCHAR(32) NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id)
                )
            '''))
        with self.assertRaises(SupportTicketNotificationMigrationError):
            migrate(self.engine, schema=self.schema)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT 1")).scalar_one(), 1)


if __name__ == "__main__":
    unittest.main()
