"""Opt-in PostgreSQL concurrency coverage for Phase F owner ticket commands."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.brain.memory_manager import MemoryManager
from app.core.config import settings
from app.db.models.customer import Customer
from app.db.models.support_ticket import SAFE_TICKET_SUMMARIES, SupportTicket
from app.db.models.support_ticket_notification import SupportTicketNotification
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.services.handoff.owner_ticket_service import OwnerTicketService
from app.services.handoff.service import HandoffService
from app.services.handoff.ticket_service import TicketService


def _database_identity(url):
    parsed = make_url(url)
    return parsed.get_backend_name(), parsed.host, parsed.port, parsed.database


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return "TEST_DATABASE_URL is not configured; Phase F PostgreSQL tests are skipped."
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if not parsed.database or "test" not in parsed.database.lower():
            return "TEST_DATABASE_URL must name a dedicated test database."
        if _database_identity(value) == _database_identity(settings.DATABASE_URL):
            return "TEST_DATABASE_URL resolves to the normal database; refusing to run."
    except Exception:
        return "TEST_DATABASE_URL is invalid; Phase F PostgreSQL tests are skipped."
    return None


SKIP_REASON = _skip_reason()


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestTelegramOwnerCommandsPostgreSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)

    def setUp(self):
        self.schema = f"aura_owner_commands_test_{uuid4().hex[:12]}"
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict(
            {"options": f"-csearch_path={self.schema},public"}
        )
        self.engine = create_engine(url, pool_pre_ping=True)
        Customer.__table__.create(self.engine)
        SupportTicket.__table__.create(self.engine)
        SupportTicketNotification.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.owner_a = uuid4()
        self.owner_b = uuid4()
        with self.Session.begin() as db:
            db.add_all([Customer(id=self.owner_a), Customer(id=self.owner_b)])

    def tearDown(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def add_ticket(self, number, *, owner=None, session_hash=None, status="open", created_at=None):
        created_at = created_at or datetime.now(timezone.utc)
        with self.Session.begin() as db:
            db.add(SupportTicket(
                ticket_number=number,
                owner_customer_id=owner or self.owner_a,
                session_reference_hash=session_hash or uuid4().hex.ljust(64, "a"),
                category="explicit_human_request",
                reason_code="explicit_human_request",
                priority="high",
                safe_summary=SAFE_TICKET_SUMMARIES["explicit_human_request"],
                status=status,
                attempt_count=1,
                created_at=created_at,
                updated_at=created_at,
                resolved_at=created_at if status in {"resolved", "closed"} else None,
            ))

    def run_operation(self, operation, number):
        db = self.Session()
        try:
            return getattr(OwnerTicketService(), operation)(db, number)
        finally:
            db.close()

    def test_active_list_order_limit_and_terminal_detail(self):
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(12):
            self.add_ticket(
                f"CS-2026-{index + 1:06d}",
                created_at=origin + timedelta(minutes=index),
            )
        self.add_ticket("CS-2026-000099", status="resolved", created_at=origin - timedelta(days=1))
        db = self.Session()
        try:
            listed = OwnerTicketService().list_active_tickets(db)
            detail = OwnerTicketService().get_ticket(db, "CS-2026-000099")
        finally:
            db.close()
        self.assertEqual([item.ticket_number for item in listed.tickets], [
            f"CS-2026-{index:06d}" for index in range(1, 11)
        ])
        self.assertEqual(detail.ticket.status, "resolved")

    def test_two_take_and_two_resolve_commands_serialize(self):
        self.add_ticket("CS-2026-000001")
        with ThreadPoolExecutor(max_workers=2) as executor:
            take_codes = list(executor.map(
                lambda _index: self.run_operation("take_ticket", "CS-2026-000001"),
                range(2),
            ))
        self.assertEqual(sorted(result.code for result in take_codes), ["already_in_progress", "success"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            resolve_codes = list(executor.map(
                lambda _index: self.run_operation("resolve_ticket", "CS-2026-000001"),
                range(2),
            ))
        self.assertEqual(sorted(result.code for result in resolve_codes), ["already_resolved", "success"])

    def test_take_racing_resolve_finishes_resolved_and_never_reopens(self):
        self.add_ticket("CS-2026-000002")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.run_operation, "take_ticket", "CS-2026-000002"),
                executor.submit(self.run_operation, "resolve_ticket", "CS-2026-000002"),
            ]
            results = [future.result(timeout=15) for future in futures]
        with self.Session() as db:
            ticket = db.scalar(select(SupportTicket).where(SupportTicket.ticket_number == "CS-2026-000002"))
            self.assertEqual(ticket.status, "resolved")
            self.assertIsNotNone(ticket.resolved_at)
            self.assertEqual(ticket.updated_at, ticket.resolved_at)
            resolved_at = ticket.resolved_at
        self.assertIn("success", {result.code for result in results})
        self.assertEqual(self.run_operation("take_ticket", "CS-2026-000002").code, "not_available")
        with self.Session() as db:
            ticket = db.scalar(select(SupportTicket).where(SupportTicket.ticket_number == "CS-2026-000002"))
            self.assertEqual(ticket.resolved_at, resolved_at)

    def test_rollback_leaves_real_session_usable(self):
        class FailingRepository(SupportTicketRepository):
            def get_for_owner_transition(self, db, *, ticket_number):
                db.execute(text("SELECT 1 / 0"))

        db = self.Session()
        try:
            result = OwnerTicketService(FailingRepository()).take_ticket(db, "CS-2026-000001")
            self.assertEqual(result.code, "database_error")
            self.assertEqual(db.scalar(select(func.count()).select_from(Customer)), 2)
        finally:
            db.close()

    def test_resolution_clears_only_matching_running_lock_and_restart_ignores_terminal(self):
        raw_session = "shared-session"
        key_a = f"{self.owner_a}:{raw_session}"
        key_b = f"{self.owner_b}:{raw_session}"
        self.add_ticket(
            "CS-2026-000010", owner=self.owner_a,
            session_hash=TicketService.hash_session_reference(key_a),
        )
        self.add_ticket(
            "CS-2026-000011", owner=self.owner_b,
            session_hash=TicketService.hash_session_reference(key_b),
        )
        memory = MemoryManager()
        handoff = HandoffService(memory)
        for key in (key_a, key_b):
            memory.get_session(key).update({
                "handoff_required": True,
                "handoff_state": {"status": "open"},
                "update_reservation_stage": "input_value",
            })

        db = self.Session()
        try:
            self.assertEqual(OwnerTicketService().resolve_ticket(db, "CS-2026-000010").code, "success")
            handoff.restore_active_handoff(key_a, db, self.owner_a)
            handoff.restore_active_handoff(key_b, db, self.owner_b)
        finally:
            db.close()
        self.assertFalse(memory.get_session(key_a).get("handoff_required"))
        self.assertEqual(memory.get_session(key_a)["update_reservation_stage"], "input_value")
        self.assertTrue(memory.get_session(key_b)["handoff_required"])

        restarted = HandoffService(MemoryManager())
        db = self.Session()
        try:
            self.assertIsNone(restarted.restore_active_handoff(key_a, db, self.owner_a))
            self.assertIsNotNone(restarted.restore_active_handoff(key_b, db, self.owner_b))
        finally:
            db.close()

    def test_owner_operations_create_zero_notification_rows(self):
        self.add_ticket("CS-2026-000020")
        db = self.Session()
        service = OwnerTicketService()
        try:
            service.list_active_tickets(db)
            service.get_ticket(db, "CS-2026-000020")
            service.take_ticket(db, "CS-2026-000020")
            service.resolve_ticket(db, "CS-2026-000020")
            service.resolve_ticket(db, "CS-2026-000020")
            count = db.scalar(select(func.count()).select_from(SupportTicketNotification))
        finally:
            db.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
