"""Opt-in PostgreSQL concurrency coverage for V2.0 G1C.

These tests verify database convergence around the in-process conversation
lock. They run only against an explicit disposable TEST_DATABASE_URL and never
fall back to the normal application database.
"""

import asyncio
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.brain.memory_manager import MemoryManager
from app.core.config import settings
from app.core.conversation_lock_manager import ConversationLockManager
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.transaction_errors import PersistenceOperationError
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.handoff.owner_ticket_service import OwnerTicketService
from app.services.handoff.service import HandoffService
from app.services.handoff.ticket_service import TicketService
from migrations.add_support_ticket_notifications import (
    migrate as migrate_notifications,
)
from migrations.add_support_tickets import migrate as migrate_tickets
from tests.integration.disposable_schema import DisposableSchemaResources


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
        return "TEST_DATABASE_URL is not configured; G1C PostgreSQL tests are skipped."
    try:
        parsed = make_url(test_url)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if not parsed.database or "test" not in parsed.database.lower():
            return "TEST_DATABASE_URL must name a disposable database containing 'test'."
        if _database_identity(test_url) == _database_identity(settings.DATABASE_URL):
            return "TEST_DATABASE_URL resolves to the normal application database; refusing to run."
    except Exception:
        return "TEST_DATABASE_URL is invalid; G1C PostgreSQL tests are skipped."
    return None


SKIP_REASON = _integration_skip_reason()


def _handoff_state():
    return {
        "category": "explicit_human_request",
        "reason_code": "explicit_human_request",
        "priority": "high",
        "attempt_count": 1,
    }


class ExplicitHandoffAgent:
    """Small adapter around the real handoff/ticket/outbox services."""

    def __init__(self, handoff_service, *, pause_first=False):
        self.handoff_service = handoff_service
        self.pause_first = pause_first
        self.first_entered = asyncio.Event()
        self.release_first = asyncio.Event()
        self._paused = False

    async def handle(self, *, session_id, message, db, owner_customer_id):
        if self.pause_first and not self._paused:
            self._paused = True
            self.first_entered.set()
            await self.release_first.wait()
        if self.handoff_service.is_required(session_id):
            return self.handoff_service.waiting_response(session_id)
        if message != "petugas":
            return "normal"
        self.handoff_service.require_handoff(
            session_id,
            "explicit_human_request",
            db=db,
            owner_customer_id=owner_customer_id,
        )
        return self.handoff_service.explicit_response(session_id)


class NoopRecoveryHandoff:
    def restore_active_handoff(self, *_args):
        return None

    @staticmethod
    def recovery_error_response():
        return "safe recovery response"


class TicketCreatingAgent:
    def __init__(self, ticket_services):
        self.handoff_service = NoopRecoveryHandoff()
        self.ticket_services = iter(ticket_services)

    async def handle(self, *, session_id, db, owner_customer_id, **_kwargs):
        service = next(self.ticket_services)
        ticket = service.create_or_get(
            db,
            owner_customer_id=owner_customer_id,
            memory_key=session_id,
            handoff_state=_handoff_state(),
        )
        return ticket.ticket_number


class FailingDatabaseNotification:
    def enqueue_new_ticket(self, db, *, ticket):
        del ticket
        db.execute(text('SELECT 1 FROM "g1c_controlled_missing_table"'))


class BarrierRepository(SupportTicketRepository):
    """Force independent managers through the same SELECT -> INSERT race."""

    def __init__(self, barrier):
        self.barrier = barrier
        self.local = threading.local()

    def get_active_by_owner_and_session_hash(
        self,
        db,
        owner_customer_id,
        session_reference_hash,
    ):
        ticket = super().get_active_by_owner_and_session_hash(
            db,
            owner_customer_id,
            session_reference_hash,
        )
        if ticket is None and not getattr(self.local, "lookup_paused", False):
            self.local.lookup_paused = True
            self.barrier.wait(timeout=10)
        return ticket


class PausingActiveTicketRepository(SupportTicketRepository):
    """Pause after reading an active row so an owner can resolve it."""

    def __init__(self, ticket_read, allow_return):
        self.ticket_read = ticket_read
        self.allow_return = allow_return
        self._paused = False

    def get_active_by_owner_and_session_hash(
        self,
        db,
        owner_customer_id,
        session_reference_hash,
    ):
        ticket = super().get_active_by_owner_and_session_hash(
            db,
            owner_customer_id,
            session_reference_hash,
        )
        if ticket is not None and not self._paused:
            self._paused = True
            self.ticket_read.set()
            if not self.allow_return.wait(timeout=10):
                raise TimeoutError("Controlled owner/customer race did not resume.")
        return ticket


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestConversationSerializationPostgreSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_url = os.environ["TEST_DATABASE_URL"]
        cls.admin_engine = create_engine(cls.test_url, pool_pre_ping=True)
        cls.schema = f"aura_g1c_test_{uuid4().hex[:12]}"
        cls.schema_resources = DisposableSchemaResources(
            admin_engine=cls.admin_engine,
            schema=cls.schema,
            allowed_prefixes=("aura_g1c_test_",),
            dispose_admin=True,
        )
        cls.addClassCleanup(cls.schema_resources.cleanup)
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
            connection.execute(text(f"""
                CREATE TABLE "{cls.schema}".customers (
                    id UUID PRIMARY KEY,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))

        schema_url = make_url(cls.test_url).update_query_dict({
            "options": f"-csearch_path={cls.schema},public",
        })
        cls.engine = create_engine(schema_url, pool_pre_ping=True)
        cls.schema_resources.track_engine(cls.engine)
        cls.SessionLocal = sessionmaker(
            bind=cls.engine,
            autoflush=False,
            autocommit=False,
        )
        migrate_tickets(cls.engine, schema=cls.schema)
        migrate_notifications(cls.engine, schema=cls.schema)

    @classmethod
    def _table(cls, name):
        return f'"{cls.schema}"."{name}"'

    @classmethod
    def _insert_customer(cls):
        customer_id = uuid4()
        with cls.engine.begin() as connection:
            connection.execute(
                text(f"""
                    INSERT INTO {cls._table('customers')}
                        (id, token_version, is_active)
                    VALUES (:id, 1, TRUE)
                """),
                {"id": customer_id},
            )
        return customer_id

    def _counts(self, owner_customer_id, memory_key):
        session_hash = TicketService.hash_session_reference(memory_key)
        with self.engine.connect() as connection:
            tickets = connection.execute(text(f"""
                SELECT COUNT(*)
                FROM {self._table('support_tickets')}
                WHERE owner_customer_id = :owner
                  AND session_reference_hash = :session_hash
                  AND status IN ('open', 'in_progress')
            """), {
                "owner": owner_customer_id,
                "session_hash": session_hash,
            }).scalar_one()
            notifications = connection.execute(text(f"""
                SELECT COUNT(*)
                FROM {self._table('support_ticket_notifications')} AS notification
                JOIN {self._table('support_tickets')} AS ticket
                  ON ticket.id = notification.support_ticket_id
                WHERE ticket.owner_customer_id = :owner
                  AND ticket.session_reference_hash = :session_hash
                  AND notification.status = 'pending'
            """), {
                "owner": owner_customer_id,
                "session_hash": session_hash,
            }).scalar_one()
        return tickets, notifications

    def test_same_session_concurrent_handoff_creates_one_ticket_and_notification(self):
        owner = self._insert_customer()
        customer = SimpleNamespace(id=owner)
        session_reference = f"serialized-{uuid4().hex}"
        memory_key = build_authenticated_memory_key(owner, session_reference)
        memory = MemoryManager()
        agent = ExplicitHandoffAgent(
            HandoffService(memory),
            pause_first=True,
        )
        service = AuthenticatedChatService(
            agent=agent,
            lock_manager=ConversationLockManager(),
        )

        async def scenario():
            first_db = self.SessionLocal()
            second_db = self.SessionLocal()
            try:
                first = asyncio.create_task(service.process(
                    db=first_db,
                    customer=customer,
                    session_reference=session_reference,
                    message="petugas",
                ))
                await agent.first_entered.wait()
                second = asyncio.create_task(service.process(
                    db=second_db,
                    customer=customer,
                    session_reference=session_reference,
                    message="petugas",
                ))
                await asyncio.sleep(0)
                self.assertFalse(second.done())
                agent.release_first.set()
                return await asyncio.wait_for(
                    asyncio.gather(first, second),
                    timeout=10,
                )
            finally:
                first_db.close()
                second_db.close()

        first_response, second_response = asyncio.run(scenario())
        self.assertIn("Nomor tiket Anda: CS-", first_response)
        number = first_response.rsplit("Nomor tiket Anda: ", 1)[1]
        self.assertIn(number, second_response)
        self.assertEqual(self._counts(owner, memory_key), (1, 1))

    def test_database_failure_rolls_back_releases_lock_and_next_message_succeeds(self):
        owner = self._insert_customer()
        customer = SimpleNamespace(id=owner)
        session_reference = f"failure-{uuid4().hex}"
        memory_key = build_authenticated_memory_key(owner, session_reference)
        failing = TicketService(
            repository=SupportTicketRepository(),
            notification_service=FailingDatabaseNotification(),
        )
        succeeding = TicketService()
        agent = TicketCreatingAgent((failing, succeeding))
        manager = ConversationLockManager()
        service = AuthenticatedChatService(agent=agent, lock_manager=manager)
        db = self.SessionLocal()
        try:
            with self.assertRaises(PersistenceOperationError) as raised:
                asyncio.run(service.process(
                    db=db,
                    customer=customer,
                    session_reference=session_reference,
                    message="first",
                ))
            rendered = str(raised.exception)
            self.assertEqual(rendered, "PERSISTENCE_OPERATION_FAILED")
            self.assertNotIn("g1c_controlled_missing_table", rendered)
            self.assertNotIn("SELECT", rendered)
            self.assertEqual(db.execute(text("SELECT 1")).scalar_one(), 1)

            response = asyncio.run(service.process(
                db=db,
                customer=customer,
                session_reference=session_reference,
                message="second",
            ))
            self.assertTrue(response.startswith("CS-"))
            self.assertEqual(db.execute(text("SELECT 1")).scalar_one(), 1)
        finally:
            db.close()

        self.assertEqual(manager.registry_size_for_test, 0)
        self.assertEqual(self._counts(owner, memory_key), (1, 1))

    def test_independent_managers_rely_on_database_convergence(self):
        owner = self._insert_customer()
        customer = SimpleNamespace(id=owner)
        session_reference = f"restart-{uuid4().hex}"
        memory_key = build_authenticated_memory_key(owner, session_reference)
        repository = BarrierRepository(threading.Barrier(2))

        def create_with_independent_manager():
            db = self.SessionLocal()
            try:
                ticket_service = TicketService(repository=repository)
                service = AuthenticatedChatService(
                    agent=TicketCreatingAgent((ticket_service,)),
                    lock_manager=ConversationLockManager(),
                )
                return asyncio.run(service.process(
                    db=db,
                    customer=customer,
                    session_reference=session_reference,
                    message="petugas",
                ))
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(
                lambda _index: create_with_independent_manager(),
                range(2),
            ))

        self.assertEqual(responses[0], responses[1])
        self.assertTrue(responses[0].startswith("CS-"))
        self.assertEqual(self._counts(owner, memory_key), (1, 1))

    def test_owner_resolve_race_keeps_terminal_ticket_and_next_message_reconciles(self):
        owner = self._insert_customer()
        customer = SimpleNamespace(id=owner)
        session_reference = f"resolve-race-{uuid4().hex}"
        memory_key = build_authenticated_memory_key(owner, session_reference)
        setup_db = self.SessionLocal()
        try:
            ticket = TicketService().create_or_get(
                setup_db,
                owner_customer_id=owner,
                memory_key=memory_key,
                handoff_state=_handoff_state(),
            )
            ticket_number = ticket.ticket_number
        finally:
            setup_db.close()

        ticket_read = threading.Event()
        allow_customer = threading.Event()
        memory = MemoryManager()
        pausing_ticket_service = TicketService(
            repository=PausingActiveTicketRepository(
                ticket_read,
                allow_customer,
            ),
        )
        stale_service = AuthenticatedChatService(
            agent=ExplicitHandoffAgent(
                HandoffService(memory, ticket_service=pausing_ticket_service)
            ),
            lock_manager=ConversationLockManager(),
        )

        def customer_message():
            db = self.SessionLocal()
            try:
                return asyncio.run(stale_service.process(
                    db=db,
                    customer=customer,
                    session_reference=session_reference,
                    message="halo",
                ))
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(customer_message)
            self.assertTrue(ticket_read.wait(timeout=10))
            owner_db = self.SessionLocal()
            try:
                resolved = OwnerTicketService().resolve_ticket(
                    owner_db,
                    ticket_number,
                )
                self.assertEqual(resolved.code, "success")
                self.assertEqual(resolved.ticket.status, "resolved")
            finally:
                owner_db.close()
            allow_customer.set()
            stale_response = future.result(timeout=10)

        self.assertIn("menunggu bantuan petugas", stale_response)
        self.assertIn(ticket_number, stale_response)

        reconciled_service = AuthenticatedChatService(
            agent=ExplicitHandoffAgent(HandoffService(memory)),
            lock_manager=ConversationLockManager(),
        )
        next_db = self.SessionLocal()
        try:
            next_response = asyncio.run(reconciled_service.process(
                db=next_db,
                customer=customer,
                session_reference=session_reference,
                message="halo",
            ))
        finally:
            next_db.close()

        self.assertEqual(next_response, "normal")
        self.assertFalse(HandoffService(memory).is_required(memory_key))
        with self.engine.connect() as connection:
            status = connection.execute(text(f"""
                SELECT status
                FROM {self._table('support_tickets')}
                WHERE ticket_number = :ticket_number
            """), {"ticket_number": ticket_number}).scalar_one()
        self.assertEqual(status, "resolved")


if __name__ == "__main__":
    unittest.main()
