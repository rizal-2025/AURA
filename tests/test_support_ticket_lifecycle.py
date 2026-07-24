import asyncio
import io
import logging
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agents.orchestrator import AgentOrchestrator
from app.api.chat import agent as chat_agent
from app.api.chat import chat
from app.brain.memory_manager import MemoryManager
from app.core.ownership import MissingOwnerCustomerError
from app.db.models.support_ticket import SupportTicket
from app.schemas.chat import ChatRequest
from app.services.handoff.service import HandoffService
from app.services.handoff.ticket_service import TicketService


class TransactionDB:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class InMemoryLifecycleRepository:
    def __init__(self):
        self.tickets = []
        self.create_count = 0
        self.lookup_count = 0

    def get_active_by_owner_and_session_hash(self, _db, owner_customer_id, session_hash):
        self.lookup_count += 1
        for ticket in reversed(self.tickets):
            if (
                ticket.owner_customer_id == owner_customer_id
                and ticket.session_reference_hash == session_hash
                and ticket.status in {"open", "in_progress"}
            ):
                return ticket
        return None

    def create(self, _db, **kwargs):
        self.create_count += 1
        ticket = SimpleNamespace(
            id=len(self.tickets) + 1,
            ticket_number=f"CS-2026-{len(self.tickets) + 1:06d}",
            owner_customer_id=kwargs["owner_customer_id"],
            session_reference_hash=kwargs["session_reference_hash"],
            category=kwargs["category"],
            reason_code=kwargs["reason_code"],
            priority=kwargs["priority"],
            safe_summary="not trusted during recovery",
            status="open",
            attempt_count=kwargs["attempt_count"],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            resolved_at=None,
        )
        self.tickets.append(ticket)
        return ticket

    def _set_status(self, ticket_id, owner_customer_id, status):
        for ticket in self.tickets:
            if ticket.id == ticket_id and ticket.owner_customer_id == owner_customer_id:
                ticket.status = status
                ticket.updated_at = datetime.now(timezone.utc)
                ticket.resolved_at = (
                    ticket.updated_at if status in {"resolved", "closed"} else None
                )
                return ticket
        return None

    def mark_in_progress(self, _db, *, ticket_id, owner_customer_id):
        return self._set_status(ticket_id, owner_customer_id, "in_progress")

    def resolve(self, _db, *, ticket_id, owner_customer_id):
        return self._set_status(ticket_id, owner_customer_id, "resolved")

    def close(self, _db, *, ticket_id, owner_customer_id):
        return self._set_status(ticket_id, owner_customer_id, "closed")

    def update_status(self, _db, *, ticket_id, owner_customer_id, status):
        return self._set_status(ticket_id, owner_customer_id, status)


def handoff_state(category="explicit_human_request"):
    return {
        "category": category,
        "reason_code": category,
        "priority": "high" if category == "explicit_human_request" else "medium",
        "safe_summary": "caller text is ignored",
        "attempt_count": 1,
    }


class TestSupportTicketLifecycle(unittest.TestCase):
    def test_model_declares_checks_and_active_partial_unique_index(self):
        constraint_names = {
            constraint.name for constraint in SupportTicket.__table__.constraints
        }
        self.assertIn("ck_support_tickets_priority", constraint_names)
        self.assertIn("ck_support_tickets_status", constraint_names)
        self.assertIn("uq_support_tickets_ticket_number", constraint_names)

        active_index = next(
            index
            for index in SupportTicket.__table__.indexes
            if index.name == "uq_support_tickets_active_owner_session"
        )
        self.assertTrue(active_index.unique)
        self.assertEqual(
            [column.name for column in active_index.columns],
            ["owner_customer_id", "session_reference_hash"],
        )
        predicate = str(active_index.dialect_options["postgresql"]["where"])
        self.assertIn("open", predicate)
        self.assertIn("in_progress", predicate)

    def test_migration_source_is_additive_for_reservation_data(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "add_support_tickets.py"
        ).read_text(encoding="utf-8").upper()
        self.assertNotIn("DROP TABLE", source)
        self.assertNotIn("TRUNCATE", source)
        self.assertNotIn("DELETE FROM", source)
        self.assertNotIn("ALTER TABLE RESERVATIONS", source)
        self.assertIn("CK_SUPPORT_TICKETS_PRIORITY", source)
        self.assertIn("CK_SUPPORT_TICKETS_STATUS", source)
        self.assertIn("UQ_SUPPORT_TICKETS_ACTIVE_OWNER_SESSION", source)

    def test_active_lookup_excludes_resolved_and_closed_tickets(self):
        repository = InMemoryLifecycleRepository()
        service = TicketService(repository)
        owner = "customer-a"
        memory_key = "customer-a:chat-01"

        resolved = service.create_or_get(
            TransactionDB(), owner_customer_id=owner, memory_key=memory_key,
            handoff_state=handoff_state(),
        )
        service.resolve(TransactionDB(), ticket_id=resolved.id, owner_customer_id=owner)
        self.assertIsNone(
            service.get_active(TransactionDB(), owner_customer_id=owner, memory_key=memory_key)
        )

        closed = service.create_or_get(
            TransactionDB(), owner_customer_id=owner, memory_key=memory_key,
            handoff_state=handoff_state(),
        )
        service.close(TransactionDB(), ticket_id=closed.id, owner_customer_id=owner)
        self.assertIsNone(
            service.get_active(TransactionDB(), owner_customer_id=owner, memory_key=memory_key)
        )

    def test_resolved_and_closed_tickets_allow_new_ticket_numbers(self):
        for terminal_status in ("resolved", "closed"):
            with self.subTest(status=terminal_status):
                repository = InMemoryLifecycleRepository()
                service = TicketService(repository)
                owner = "customer-a"
                memory_key = f"customer-a:{terminal_status}"
                first = service.create_or_get(
                    TransactionDB(), owner_customer_id=owner, memory_key=memory_key,
                    handoff_state=handoff_state(),
                )
                getattr(service, "resolve" if terminal_status == "resolved" else "close")(
                    TransactionDB(), ticket_id=first.id, owner_customer_id=owner,
                )
                second = service.create_or_get(
                    TransactionDB(), owner_customer_id=owner, memory_key=memory_key,
                    handoff_state=handoff_state(),
                )
                self.assertNotEqual(first.ticket_number, second.ticket_number)
                self.assertEqual(repository.create_count, 2)

    def test_mark_in_progress_keeps_ticket_active(self):
        repository = InMemoryLifecycleRepository()
        service = TicketService(repository)
        owner = "customer-a"
        memory_key = "customer-a:in-progress"
        ticket = service.create_or_get(
            TransactionDB(), owner_customer_id=owner, memory_key=memory_key,
            handoff_state=handoff_state(),
        )
        updated = service.mark_in_progress(
            TransactionDB(), ticket_id=ticket.id, owner_customer_id=owner,
        )
        self.assertEqual(updated.status, "in_progress")
        self.assertIsNone(updated.resolved_at)
        active = service.get_active(
            TransactionDB(),
            owner_customer_id=owner,
            memory_key=memory_key,
        )
        self.assertEqual(active.id, ticket.id)
        self.assertEqual(active.status, "in_progress")

    def test_restart_recovery_restores_lock_and_blocks_update_and_cancel(self):
        repository = InMemoryLifecycleRepository()
        ticket_service = TicketService(repository)
        owner = "customer-a"
        raw_session = "private-session"
        memory_key = f"{owner}:{raw_session}"

        initial = HandoffService(MemoryManager(), ticket_service=ticket_service)
        initial.require_handoff(
            memory_key,
            "explicit_human_request",
            db=TransactionDB(),
            owner_customer_id=owner,
        )

        restarted_memory = MemoryManager()
        restarted_handoff = HandoffService(
            restarted_memory,
            ticket_service=ticket_service,
        )
        restored = restarted_handoff.restore_active_handoff(
            memory_key,
            TransactionDB(),
            owner,
        )
        self.assertTrue(restored["handoff_required"])
        self.assertEqual(restored["ticket_number"], "CS-2026-000001")
        self.assertNotIn(raw_session, str(restored))
        self.assertNotIn(owner, str(restored))

        orchestrator = AgentOrchestrator()
        orchestrator.memory_manager = restarted_memory
        orchestrator.handoff_service = restarted_handoff
        orchestrator.update_reservation_agent.run = AsyncMock()
        orchestrator.cancel_reservation_agent.run = AsyncMock()

        update_reply = asyncio.run(orchestrator.handle(
            memory_key, "ubah reservasi saya", TransactionDB(), owner,
        ))
        cancel_reply = asyncio.run(orchestrator.handle(
            memory_key, "batalkan reservasi saya", TransactionDB(), owner,
        ))
        self.assertIn("menunggu bantuan petugas", update_reply)
        self.assertIn("CS-2026-000001", update_reply)
        self.assertIn("menunggu bantuan petugas", cancel_reply)
        orchestrator.update_reservation_agent.run.assert_not_awaited()
        orchestrator.cancel_reservation_agent.run.assert_not_awaited()
        self.assertEqual(repository.create_count, 1)

    def test_restart_recovery_is_customer_scoped_for_same_raw_session(self):
        repository = InMemoryLifecycleRepository()
        service = TicketService(repository)
        session_id = "shared-session"
        key_a = f"customer-a:{session_id}"
        key_b = f"customer-b:{session_id}"
        first_memory = MemoryManager()
        HandoffService(first_memory, ticket_service=service).require_handoff(
            key_a,
            "explicit_human_request",
            db=TransactionDB(),
            owner_customer_id="customer-a",
        )

        restarted = HandoffService(MemoryManager(), ticket_service=service)
        self.assertIsNotNone(
            restarted.restore_active_handoff(key_a, TransactionDB(), "customer-a")
        )
        self.assertIsNone(
            restarted.restore_active_handoff(key_b, TransactionDB(), "customer-b")
        )
        self.assertFalse(restarted.is_required(key_b))

    def test_recovery_missing_owner_fails_before_repository_lookup(self):
        repository = InMemoryLifecycleRepository()
        handoff = HandoffService(
            MemoryManager(),
            ticket_service=TicketService(repository),
        )
        with self.assertRaises(MissingOwnerCustomerError):
            handoff.restore_active_handoff("unscoped", TransactionDB(), None)
        self.assertEqual(repository.lookup_count, 0)

    def test_recovery_database_error_returns_safe_response_before_workflow(self):
        customer = SimpleNamespace(id="customer-a")
        database_error = "postgres password=secret stack trace"
        request = ChatRequest(session_id="private-session", message="ubah reservasi saya")
        with (
            patch.object(
                chat_agent.handoff_service,
                "restore_active_handoff",
                side_effect=RuntimeError(database_error),
            ),
            patch.object(chat_agent, "handle", AsyncMock()) as handle,
        ):
            response = asyncio.run(chat(request, db=TransactionDB(), current_customer=customer))

        self.assertIn("belum dapat diperiksa", response.reply)
        self.assertNotIn(database_error, response.reply)
        self.assertNotIn("private-session", response.reply)
        handle.assert_not_awaited()

    def test_recovery_does_not_log_or_restore_private_identity_values(self):
        repository = InMemoryLifecycleRepository()
        service = TicketService(repository)
        owner = "private-owner-uuid"
        raw_session = "private-raw-session"
        memory_key = f"{owner}:{raw_session}"
        HandoffService(MemoryManager(), ticket_service=service).require_handoff(
            memory_key,
            "explicit_human_request",
            db=TransactionDB(),
            owner_customer_id=owner,
        )

        restarted = HandoffService(MemoryManager(), ticket_service=service)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        aura_logger = logging.getLogger("AURA")
        aura_logger.addHandler(handler)
        try:
            restored = restarted.restore_active_handoff(memory_key, TransactionDB(), owner)
            reply = restarted.waiting_response(memory_key)
        finally:
            aura_logger.removeHandler(handler)

        combined = f"{restored} {reply} {stream.getvalue()}"
        self.assertNotIn(raw_session, combined)
        self.assertNotIn(owner, combined)
        self.assertNotIn("not trusted during recovery", combined)


if __name__ == "__main__":
    unittest.main()
