import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api.chat import agent as chat_agent
from app.core.config import settings
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.security import create_customer_access_token
from app.db.database import get_db
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.main import app
from app.services.handoff.ticket_service import TicketService
from migrations.add_support_tickets import migrate as migrate_support_tickets
from migrations.add_support_ticket_notifications import migrate as migrate_notifications


def migrate(target_engine, *, schema=None):
    ticket_changed = migrate_support_tickets(target_engine, schema=schema)
    migrate_notifications(target_engine, schema=schema)
    return ticket_changed


def _database_identity(url):
    parsed = make_url(url)
    return (
        parsed.get_backend_name(),
        parsed.host,
        parsed.port,
        parsed.database,
    )


def _integration_skip_reason():
    test_url = os.getenv("TEST_DATABASE_URL")
    if not test_url:
        return "TEST_DATABASE_URL is not configured; dedicated PostgreSQL tests are skipped."
    try:
        parsed = make_url(test_url)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if not parsed.database or "test" not in parsed.database.lower():
            return "TEST_DATABASE_URL must name a dedicated database containing 'test'."
        if _database_identity(test_url) == _database_identity(settings.DATABASE_URL):
            return "TEST_DATABASE_URL resolves to the normal application database; refusing to run."
    except Exception:
        return "TEST_DATABASE_URL is invalid; dedicated PostgreSQL tests are skipped."
    return None


SKIP_REASON = _integration_skip_reason()


class BarrierRepository(SupportTicketRepository):
    def __init__(self, barrier):
        self.barrier = barrier
        self.local = threading.local()

    def get_active_by_owner_and_session_hash(self, db, owner_customer_id, session_reference_hash):
        ticket = super().get_active_by_owner_and_session_hash(
            db,
            owner_customer_id,
            session_reference_hash,
        )
        if ticket is None and not getattr(self.local, "initial_lookup_complete", False):
            self.local.initial_lookup_complete = True
            self.barrier.wait(timeout=10)
        return ticket


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestSupportTicketsPostgreSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_url = os.environ["TEST_DATABASE_URL"]
        cls.admin_engine = create_engine(cls.test_url, pool_pre_ping=True)
        cls.schema = f"aura_support_ticket_test_{uuid4().hex[:12]}"
        schema_name = f'"{cls.schema}"'
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema_name}"))
            connection.execute(text(f"""
                CREATE TABLE {schema_name}.customers (
                    id UUID PRIMARY KEY,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            connection.execute(text(f"""
                CREATE TABLE {schema_name}.reservations (
                    id SERIAL PRIMARY KEY,
                    marker VARCHAR(64) NOT NULL
                )
            """))
            connection.execute(text(
                f"INSERT INTO {schema_name}.reservations (marker) VALUES ('must-remain')"
            ))

        schema_url = make_url(cls.test_url).update_query_dict({
            "options": f"-csearch_path={cls.schema},public",
        })
        cls.engine = create_engine(schema_url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin_engine.dispose()

    @classmethod
    def _table(cls, name):
        return f'"{cls.schema}"."{name}"'

    @classmethod
    def _insert_customer(cls, customer_id=None):
        customer_id = customer_id or uuid4()
        with cls.engine.begin() as connection:
            connection.execute(
                text(f"""
                    INSERT INTO {cls._table('customers')}
                        (id, token_version, is_active)
                    VALUES (:id, 1, TRUE)
                    ON CONFLICT (id) DO NOTHING
                """),
                {"id": customer_id},
            )
        return customer_id

    @classmethod
    def _insert_ticket(cls, *, owner, session_hash, status="open", priority="high", ticket_number=None):
        ticket_number = ticket_number or f"CS-TEST-{uuid4().hex[:12]}"
        with cls.engine.begin() as connection:
            return connection.execute(
                text(f"""
                    INSERT INTO {cls._table('support_tickets')} (
                        ticket_number, owner_customer_id, session_reference_hash,
                        category, reason_code, priority, safe_summary, status,
                        attempt_count
                    ) VALUES (
                        :ticket_number, :owner, :session_hash,
                        'explicit_human_request', 'explicit_human_request',
                        :priority, 'Customer requested human assistance.', :status, 1
                    )
                    RETURNING id, ticket_number
                """),
                {
                    "ticket_number": ticket_number,
                    "owner": owner,
                    "session_hash": session_hash,
                    "priority": priority,
                    "status": status,
                },
            ).one()

    def test_01_migration_converges_and_restores_missing_index_without_touching_reservations(self):
        with self.engine.connect() as connection:
            before = connection.execute(
                text(f"SELECT id, marker FROM {self._table('reservations')} ORDER BY id")
            ).all()
        self.assertTrue(migrate(self.engine, schema=self.schema))
        self.assertFalse(migrate(self.engine, schema=self.schema))

        with self.engine.begin() as connection:
            connection.execute(text(
                f'DROP INDEX "{self.schema}"."ix_support_tickets_created_at"'
            ))
        self.assertTrue(migrate(self.engine, schema=self.schema))

        inspector = inspect(self.engine)
        index_names = {
            index["name"]
            for index in inspector.get_indexes("support_tickets", schema=self.schema)
        }
        with self.engine.connect() as connection:
            after = connection.execute(
                text(f"SELECT id, marker FROM {self._table('reservations')} ORDER BY id")
            ).all()
        self.assertIn("ix_support_tickets_created_at", index_names)
        self.assertEqual(before, after)

    def test_02_schema_and_constraints_are_enforced(self):
        migrate(self.engine, schema=self.schema)
        inspector = inspect(self.engine)
        columns = {
            column["name"]: column
            for column in inspector.get_columns("support_tickets", schema=self.schema)
        }
        self.assertEqual(set(columns), {
            "id", "ticket_number", "owner_customer_id", "session_reference_hash",
            "category", "reason_code", "priority", "safe_summary", "status",
            "attempt_count", "created_at", "updated_at", "resolved_at",
        })
        self.assertTrue(columns["created_at"]["type"].timezone)
        self.assertTrue(columns["updated_at"]["type"].timezone)
        self.assertTrue(columns["resolved_at"]["type"].timezone)
        self.assertEqual(
            inspector.get_pk_constraint("support_tickets", schema=self.schema)["constrained_columns"],
            ["id"],
        )

        check_names = {
            check["name"]
            for check in inspector.get_check_constraints("support_tickets", schema=self.schema)
        }
        self.assertIn("ck_support_tickets_priority", check_names)
        self.assertIn("ck_support_tickets_status", check_names)
        foreign_keys = inspector.get_foreign_keys("support_tickets", schema=self.schema)
        self.assertTrue(any(
            foreign_key["constrained_columns"] == ["owner_customer_id"]
            and foreign_key["referred_table"] == "customers"
            for foreign_key in foreign_keys
        ))
        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("support_tickets", schema=self.schema)
        }
        active = indexes["uq_support_tickets_active_owner_session"]
        self.assertTrue(active["unique"])
        self.assertIn("postgresql_where", active["dialect_options"])
        for expected in (
            "ix_support_tickets_ticket_number",
            "ix_support_tickets_owner_customer_id",
            "ix_support_tickets_status",
            "ix_support_tickets_created_at",
        ):
            self.assertIn(expected, indexes)

        owner = self._insert_customer()
        invalid_cases = (
            {"priority": "normal", "status": "open", "owner": owner},
            {"priority": "high", "status": "invalid", "owner": owner},
            {"priority": "high", "status": "open", "owner": None},
        )
        for index, values in enumerate(invalid_cases):
            with self.subTest(values=values), self.assertRaises(IntegrityError):
                self._insert_ticket(
                    owner=values["owner"],
                    session_hash=f"invalid-{index}".ljust(64, "0"),
                    priority=values["priority"],
                    status=values["status"],
                )

        duplicate_number = f"CS-TEST-{uuid4().hex[:12]}"
        self._insert_ticket(
            owner=owner,
            session_hash="unique-number-a".ljust(64, "0"),
            ticket_number=duplicate_number,
        )
        with self.assertRaises(IntegrityError):
            self._insert_ticket(
                owner=owner,
                session_hash="unique-number-b".ljust(64, "0"),
                ticket_number=duplicate_number,
            )

    def test_03_active_ticket_lifecycle_and_customer_isolation(self):
        migrate(self.engine, schema=self.schema)
        owner_a = self._insert_customer()
        owner_b = self._insert_customer()
        session_hash = uuid4().hex * 2

        first = self._insert_ticket(owner=owner_a, session_hash=session_hash)
        with self.assertRaises(IntegrityError):
            self._insert_ticket(owner=owner_a, session_hash=session_hash)
        self._insert_ticket(owner=owner_b, session_hash=session_hash)

        db = self.SessionLocal()
        service = TicketService()
        try:
            self.assertIsNone(
                service.resolve(db, ticket_id=first.id, owner_customer_id=owner_b)
            )
            resolved = service.resolve(
                db,
                ticket_id=first.id,
                owner_customer_id=owner_a,
            )
            self.assertEqual(resolved.status, "resolved")
            self.assertIsNotNone(resolved.resolved_at)
        finally:
            db.close()
        second = self._insert_ticket(owner=owner_a, session_hash=session_hash)
        db = self.SessionLocal()
        try:
            closed = service.close(
                db,
                ticket_id=second.id,
                owner_customer_id=owner_a,
            )
            self.assertEqual(closed.status, "closed")
            self.assertIsNotNone(closed.resolved_at)
        finally:
            db.close()
        third = self._insert_ticket(owner=owner_a, session_hash=session_hash)
        self.assertNotEqual(first.ticket_number, second.ticket_number)
        self.assertNotEqual(second.ticket_number, third.ticket_number)

    def test_04_concurrent_service_creation_converges_on_one_ticket(self):
        migrate(self.engine, schema=self.schema)
        owner = self._insert_customer()
        memory_key = f"{owner}:concurrent-session"
        barrier = threading.Barrier(2)
        repository = BarrierRepository(barrier)

        def create_ticket():
            db = self.SessionLocal()
            try:
                ticket = TicketService(repository).create_or_get(
                    db,
                    owner_customer_id=owner,
                    memory_key=memory_key,
                    handoff_state={
                        "category": "explicit_human_request",
                        "reason_code": "explicit_human_request",
                        "priority": "high",
                        "safe_summary": "ignored",
                        "attempt_count": 1,
                    },
                )
                return ticket.ticket_number
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            numbers = list(executor.map(lambda _item: create_ticket(), range(2)))

        session_hash = TicketService.hash_session_reference(memory_key)
        with self.engine.connect() as connection:
            rows = connection.execute(text(f"""
                SELECT ticket_number FROM {self._table('support_tickets')}
                WHERE owner_customer_id = :owner
                  AND session_reference_hash = :session_hash
                  AND status IN ('open', 'in_progress')
            """), {"owner": owner, "session_hash": session_hash}).all()
            pending_count = connection.execute(text(f"""
                SELECT COUNT(*) FROM {self._table('support_tickets')}
                WHERE ticket_number LIKE 'PENDING-%'
            """)).scalar_one()
            notification_count = connection.execute(text(f"""
                SELECT COUNT(*) FROM {self._table('support_ticket_notifications')}
            """)).scalar_one()
        self.assertEqual(numbers[0], numbers[1])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ticket_number, numbers[0])
        self.assertEqual(pending_count, 0)
        self.assertEqual(notification_count, 1)

    def test_05_integrity_failure_rolls_back_and_session_remains_usable(self):
        migrate(self.engine, schema=self.schema)
        owner = self._insert_customer()
        session_hash = uuid4().hex * 2
        repository = SupportTicketRepository()
        db = self.SessionLocal()
        try:
            repository.create(
                db,
                owner_customer_id=owner,
                session_reference_hash=session_hash,
                category="explicit_human_request",
                reason_code="explicit_human_request",
                priority="high",
                attempt_count=1,
            )
            db.commit()
            with self.assertRaises(IntegrityError):
                repository.create(
                    db,
                    owner_customer_id=owner,
                    session_reference_hash=session_hash,
                    category="explicit_human_request",
                    reason_code="explicit_human_request",
                    priority="high",
                    attempt_count=1,
                )
            self.assertEqual(db.execute(text("SELECT 1")).scalar_one(), 1)
        finally:
            db.close()

        with self.engine.connect() as connection:
            pending_count = connection.execute(text(f"""
                SELECT COUNT(*) FROM {self._table('support_tickets')}
                WHERE ticket_number LIKE 'PENDING-%'
            """)).scalar_one()
        self.assertEqual(pending_count, 0)

    def test_06_authenticated_chat_restores_lock_and_does_not_duplicate_ticket(self):
        migrate(self.engine, schema=self.schema)
        owner = self._insert_customer()
        session_id = f"restart-{uuid4().hex}"
        memory_key = build_authenticated_memory_key(owner, session_id)
        db = self.SessionLocal()
        try:
            ticket = TicketService().create_or_get(
                db,
                owner_customer_id=owner,
                memory_key=memory_key,
                handoff_state={
                    "category": "explicit_human_request",
                    "reason_code": "explicit_human_request",
                    "priority": "high",
                    "safe_summary": "ignored",
                    "attempt_count": 1,
                },
            )
        finally:
            db.close()

        chat_agent.memory_manager.clear_session(memory_key)

        def override_get_db():
            request_db = self.SessionLocal()
            try:
                yield request_db
            finally:
                request_db.close()

        app.dependency_overrides[get_db] = override_get_db
        token, _ = create_customer_access_token(owner, 1)
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            update_response = client.post(
                "/chat",
                json={"session_id": session_id, "message": "ubah reservasi saya"},
                headers=headers,
            )
            cancel_response = client.post(
                "/chat",
                json={"session_id": session_id, "message": "batalkan reservasi saya"},
                headers=headers,
            )
        finally:
            app.dependency_overrides.clear()
            chat_agent.memory_manager.clear_session(memory_key)

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(cancel_response.status_code, 200)
        self.assertIn("menunggu bantuan petugas", update_response.json()["reply"])
        self.assertIn(ticket.ticket_number, update_response.json()["reply"])
        self.assertIn("menunggu bantuan petugas", cancel_response.json()["reply"])

        session_hash = TicketService.hash_session_reference(memory_key)
        with self.engine.connect() as connection:
            count = connection.execute(text(f"""
                SELECT COUNT(*) FROM {self._table('support_tickets')}
                WHERE owner_customer_id = :owner
                  AND session_reference_hash = :session_hash
                  AND status IN ('open', 'in_progress')
            """), {"owner": owner, "session_hash": session_hash}).scalar_one()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
