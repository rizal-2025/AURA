import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.brain.memory_manager import MemoryManager
from app.core.ownership import MissingOwnerCustomerError
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.services.handoff.service import HandoffService
from app.services.handoff.ticket_service import TicketService


class FakeTicketRepository:
    def __init__(self):
        self.ticket = None
        self.created = []

    def get_active_by_owner_and_session_hash(self, _db, owner, session_hash):
        if (
            self.ticket
            and self.ticket.owner_customer_id == owner
            and self.ticket.session_reference_hash == session_hash
            and self.ticket.status in {"open", "in_progress"}
        ):
            return self.ticket
        return None

    get_by_owner_and_session_hash = get_active_by_owner_and_session_hash

    def create(self, _db, **kwargs):
        self.created.append(kwargs)
        self.ticket = SimpleNamespace(
            id=1,
            ticket_number="CS-2026-000001",
            owner_customer_id=kwargs["owner_customer_id"],
            session_reference_hash=kwargs["session_reference_hash"],
            category=kwargs["category"],
            reason_code=kwargs["reason_code"],
            priority=kwargs["priority"],
            status="open",
            safe_summary="Customer requested human assistance.",
            attempt_count=kwargs["attempt_count"],
            created_at=datetime.now(timezone.utc),
        )
        return self.ticket


class FakeTicketDB:
    def __init__(self, *, fail_on=None):
        self.fail_on = fail_on
        self.added = []
        self.rollback_calls = 0
        self.commit_snapshots = []
        self.execute_calls = 0

    def add(self, ticket):
        self.added.append(ticket)

    def flush(self):
        if self.fail_on == "flush":
            raise SQLAlchemyError("simulated flush failure")
        self.added[-1].id = len(self.added)

    def commit(self):
        if self.fail_on == "commit":
            raise SQLAlchemyError("simulated commit failure")
        ticket = self.added[-1]
        self.commit_snapshots.append((ticket.ticket_number, ticket.created_at))

    def refresh(self, _ticket):
        return None

    def rollback(self):
        self.rollback_calls += 1

    def execute(self, _statement):
        self.execute_calls += 1
        raise AssertionError("repository query should not have been executed")


class RacingTicketRepository:
    def __init__(self, winner):
        self.winner = winner
        self.lookup_count = 0
        self.create_count = 0

    def get_active_by_owner_and_session_hash(self, _db, _owner, _session_hash):
        self.lookup_count += 1
        return None if self.lookup_count == 1 else self.winner

    def create(self, _db, **_kwargs):
        self.create_count += 1
        raise IntegrityError("INSERT support_tickets", {}, Exception("unique conflict"))


class CapturingTicketRepository:
    def __init__(self):
        self.kwargs = None

    def get_active_by_owner_and_session_hash(self, _db, _owner, _session_hash):
        return None

    def create(self, _db, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(id=9, ticket_number="CS-2026-000009")


class TestSupportTickets(unittest.TestCase):
    def test_handoff_creates_one_safe_persistent_ticket_per_session(self):
        repository = FakeTicketRepository()
        memory = MemoryManager()
        handoff = HandoffService(memory, ticket_service=TicketService(repository))
        owner = "customer-a"
        memory_key = "customer-a:private-session"

        handoff.require_handoff(memory_key, "explicit_human_request", db=object(), owner_customer_id=owner)
        handoff.require_handoff(memory_key, "explicit_human_request", db=object(), owner_customer_id=owner)

        state = handoff.get_state(memory_key)
        self.assertEqual(len(repository.created), 1)
        self.assertEqual(state["ticket_number"], "CS-2026-000001")
        self.assertNotIn("private-session", repository.created[0]["session_reference_hash"])
        self.assertNotIn("safe_summary", repository.created[0])
        self.assertEqual(len(repository.created[0]["session_reference_hash"]), 64)
        self.assertEqual(repository.created[0]["priority"], "high")

    def test_hashes_are_distinct_for_isolated_customer_sessions(self):
        service = TicketService(FakeTicketRepository())
        self.assertNotEqual(
            service.hash_session_reference("customer-a:chat-01"),
            service.hash_session_reference("customer-b:chat-01"),
        )

    def test_repository_rolls_back_after_create_failure_and_session_is_usable(self):
        repository = SupportTicketRepository()
        db = FakeTicketDB(fail_on="flush")
        values = {
            "owner_customer_id": "customer-a",
            "session_reference_hash": "a" * 64,
            "category": "explicit_human_request",
            "reason_code": "explicit_human_request",
            "priority": "high",
            "attempt_count": 1,
        }

        with self.assertRaises(SQLAlchemyError):
            repository.create(db, **values)
        self.assertEqual(db.rollback_calls, 1)

        db.fail_on = None
        ticket = repository.create(db, **values)
        self.assertEqual(ticket.ticket_number, f"CS-{ticket.created_at.year}-000002")
        self.assertEqual(len(db.commit_snapshots), 1)

    def test_integrity_error_race_rolls_back_and_returns_existing_ticket(self):
        winner = SimpleNamespace(id=7, ticket_number="CS-2026-000007")
        repository = RacingTicketRepository(winner)
        db = FakeTicketDB()
        service = TicketService(repository)

        result = service.create_or_get(
            db,
            owner_customer_id="customer-a",
            memory_key="customer-a:shared-session",
            handoff_state={
                "category": "repeated_misunderstanding",
                "reason_code": "repeated_misunderstanding",
                "priority": "medium",
                "safe_summary": "raw text must be ignored",
                "attempt_count": 2,
            },
        )

        self.assertIs(result, winner)
        self.assertEqual(repository.create_count, 1)
        self.assertEqual(repository.lookup_count, 2)
        self.assertEqual(db.rollback_calls, 1)

    def test_missing_owner_is_rejected_before_ticket_repository_access(self):
        db = FakeTicketDB()
        repository = SupportTicketRepository()
        with self.assertRaises(MissingOwnerCustomerError):
            repository.get_by_owner_and_session_hash(db, None, "a" * 64)
        self.assertEqual(db.execute_calls, 0)

        service = TicketService(FakeTicketRepository())
        with self.assertRaises(MissingOwnerCustomerError):
            service.create_or_get(
                db,
                owner_customer_id=None,
                memory_key="unscoped",
                handoff_state={
                    "category": "explicit_human_request",
                    "reason_code": "explicit_human_request",
                    "priority": "high",
                    "attempt_count": 1,
                },
            )

        memory = MemoryManager()
        handoff = HandoffService(memory, ticket_service=TicketService(FakeTicketRepository()))
        with self.assertRaises(MissingOwnerCustomerError):
            handoff.require_handoff("unscoped", "explicit_human_request", db=db, owner_customer_id=None)
        self.assertNotIn("unscoped", memory._sessions)

    def test_invalid_priority_and_status_are_rejected_before_persistence(self):
        repository = SupportTicketRepository()
        db = FakeTicketDB()
        values = {
            "owner_customer_id": "customer-a",
            "session_reference_hash": "a" * 64,
            "category": "explicit_human_request",
            "reason_code": "explicit_human_request",
            "attempt_count": 1,
        }

        with self.assertRaises(ValueError):
            repository.create(db, priority="normal", **values)
        with self.assertRaises(ValueError):
            repository.create(db, priority="high", status="invalid", **values)
        self.assertEqual(db.added, [])

    def test_handoff_uses_medium_not_normal_for_non_high_priority_categories(self):
        repository = FakeTicketRepository()
        handoff = HandoffService(MemoryManager(), ticket_service=TicketService(repository))

        handoff.require_handoff(
            "customer-a:chat-01",
            "repeated_misunderstanding",
            attempt_count=2,
            db=object(),
            owner_customer_id="customer-a",
        )

        self.assertEqual(repository.created[0]["priority"], "medium")
        self.assertNotEqual(repository.created[0]["priority"], "normal")

    def test_ticket_service_ignores_raw_caller_summary(self):
        repository = CapturingTicketRepository()
        service = TicketService(repository)
        raw_text = "Rizal untuk 7 orang besok jam 19:00 dengan token rahasia"

        service.create_or_get(
            object(),
            owner_customer_id="customer-a",
            memory_key="customer-a:chat-01",
            handoff_state={
                "category": "explicit_human_request",
                "reason_code": "explicit_human_request",
                "priority": "high",
                "safe_summary": raw_text,
                "attempt_count": 1,
            },
        )

        self.assertNotIn("safe_summary", repository.kwargs)
        self.assertNotIn(raw_text, str(repository.kwargs))

    def test_ticket_number_uses_created_at_year_and_pending_value_is_never_committed(self):
        repository = SupportTicketRepository()
        db = FakeTicketDB()
        ticket = repository.create(
            db,
            owner_customer_id="customer-a",
            session_reference_hash="a" * 64,
            category="explicit_human_request",
            reason_code="explicit_human_request",
            priority="high",
            attempt_count=1,
        )

        committed_number, committed_at = db.commit_snapshots[0]
        self.assertEqual(ticket.ticket_number, committed_number)
        self.assertEqual(ticket.created_at, committed_at)
        self.assertEqual(ticket.ticket_number, f"CS-{ticket.created_at.year}-000001")
        self.assertFalse(committed_number.startswith("PENDING-"))
        self.assertTrue(committed_number)

    def test_temporary_ticket_number_fits_varchar_32(self):
        repository = SupportTicketRepository()
        db = FakeTicketDB(fail_on="flush")

        with self.assertRaises(SQLAlchemyError):
            repository.create(
                db,
                owner_customer_id="customer-a",
                session_reference_hash="a" * 64,
                category="explicit_human_request",
                reason_code="explicit_human_request",
                priority="high",
                attempt_count=1,
            )

        temporary_number = db.added[0].ticket_number
        self.assertTrue(temporary_number.startswith("PENDING-"))
        self.assertLessEqual(len(temporary_number), 32)


if __name__ == "__main__":
    unittest.main()
