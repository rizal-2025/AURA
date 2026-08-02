import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.agents.reservation_agent import ReservationAgent
from app.agents.workflow import AgentWorkflow
from app.api.error_handlers import transaction_exception_handler
from app.brain.memory_manager import MemoryManager
from app.core.customer_identity import AuthenticatedCustomer
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.core.unit_of_work import TransactionPhase, UnitOfWork
from app.db.database import get_db
from app.db.repositories.reservation_repository import ReservationRepository
from app.db.repositories.support_ticket_notification_repository import (
    SupportTicketNotificationRepository,
)
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.identity_service import TelegramIdentityService
from app.schemas.reservation import ReservationCreate
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.handoff.notification_outbox_service import NotificationOutboxService
from app.services.handoff.owner_ticket_service import OwnerTicketService
from app.services.handoff.ticket_service import TicketService
from app.services.reservation.service import ReservationService
from app.services.reservation.dto import PersistedReservationDTO


class SpySession:
    def __init__(
        self,
        *,
        commit_error=None,
        rollback_error=None,
        flush_error=None,
    ):
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.flush_error = flush_error
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self.refreshes = 0
        self.closes = 0
        self.added = []
        self.transaction_active = False

    def add(self, value):
        self.transaction_active = True
        self.added.append(value)

    def flush(self):
        self.flushes += 1
        self.transaction_active = True
        if self.flush_error is not None:
            raise self.flush_error

    def begin_nested(self):
        return nullcontext()

    def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error
        self.transaction_active = False

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error
        self.transaction_active = False

    def refresh(self, _value):
        self.refreshes += 1

    def close(self):
        self.closes += 1

    def in_transaction(self):
        return self.transaction_active


def reservation_value(identifier=17, *, people=4, status="pending"):
    return SimpleNamespace(
        id=identifier,
        name="Rizal",
        people=people,
        date="2026-08-01",
        time="19:00",
        status=status,
    )


def reservation_dto(identifier=17, *, people=4, status="pending"):
    return PersistedReservationDTO(
        id=identifier,
        name="Rizal",
        people=people,
        date="2026-08-01",
        time="19:00",
        status=status,
    )


class UnitOfWorkTests(unittest.TestCase):
    def test_commit_once_tracks_phases_and_never_closes(self):
        db = SpySession()
        with UnitOfWork(db) as unit:
            self.assertEqual(unit.phase, TransactionPhase.PRE_COMMIT)
            unit.commit()
            self.assertEqual(unit.phase, TransactionPhase.COMMITTED)
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        self.assertEqual(db.closes, 0)

    def test_pre_commit_failure_rolls_back_once(self):
        db = SpySession()
        with self.assertRaises(PersistenceOperationError):
            with UnitOfWork(db):
                raise RuntimeError("private database value")
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.commits, 0)

    def test_commit_exception_has_unknown_outcome_and_no_automatic_rollback(self):
        db = SpySession(commit_error=RuntimeError("private commit detail"))
        with self.assertRaises(PersistenceOutcomeUnknownError):
            with UnitOfWork(db) as unit:
                unit.commit()
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        with self.assertRaises(TransactionSessionUnusableError):
            with UnitOfWork(db):
                pass

    def test_rollback_failure_marks_session_unusable(self):
        db = SpySession(rollback_error=RuntimeError("private rollback detail"))
        with self.assertRaises(TransactionSessionUnusableError):
            with UnitOfWork(db):
                raise RuntimeError("private operation detail")
        with self.assertRaises(TransactionSessionUnusableError):
            with UnitOfWork(db):
                pass

    def test_base_exception_triggers_cleanup_and_is_preserved(self):
        class ControlledCancellation(BaseException):
            pass

        db = SpySession()
        with self.assertRaises(ControlledCancellation):
            with UnitOfWork(db):
                raise ControlledCancellation()
        self.assertEqual(db.rollbacks, 1)

    def test_nested_commit_ownership_is_rejected(self):
        db = SpySession()
        with UnitOfWork(db) as outer:
            with self.assertRaises(PersistenceOperationError):
                with UnitOfWork(db):
                    pass
            outer.commit()
        self.assertEqual(db.commits, 1)

    def test_public_exception_strings_and_repr_are_private(self):
        private = "postgresql://private:password@host/customer-secret"
        for error_type in (
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            error = error_type()
            rendered = f"{error!s} {error!r}"
            self.assertNotIn(private, rendered)
            self.assertIn(error.code, rendered)

    def test_get_db_rolls_back_propagated_exception_and_closes(self):
        db = SpySession()
        db.transaction_active = True
        with patch("app.db.database.SessionLocal", return_value=db):
            dependency = get_db()
            self.assertIs(next(dependency), db)
            with self.assertRaisesRegex(RuntimeError, "caller failure"):
                dependency.throw(RuntimeError("caller failure"))
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.closes, 1)

    def test_http_transaction_errors_use_stable_generic_envelopes(self):
        expected = (
            (PersistenceOperationError(), "PERSISTENCE_OPERATION_FAILED"),
            (PersistenceOutcomeUnknownError(), "PERSISTENCE_OUTCOME_UNKNOWN"),
            (
                TransactionSessionUnusableError(),
                "PERSISTENCE_SESSION_UNAVAILABLE",
            ),
        )
        for error, code in expected:
            with self.subTest(code=code):
                response = asyncio.run(transaction_exception_handler(None, error))
                self.assertEqual(response.status_code, 503)
                body = response.body.decode("utf-8")
                self.assertIn(code, body)
                self.assertNotIn("SELECT", body)
                self.assertNotIn("customer", body.lower())


class ReservationTransactionTests(unittest.TestCase):
    def test_repository_create_flushes_without_owning_transaction(self):
        db = MagicMock()
        repository = ReservationRepository()
        value = ReservationCreate(
            name="Rizal",
            people=4,
            date="2026-08-01",
            time="19:00",
        )
        repository.create(db, value, owner_customer_id=uuid4())
        db.flush.assert_called_once_with()
        db.commit.assert_not_called()
        db.rollback.assert_not_called()
        db.refresh.assert_not_called()
        db.close.assert_not_called()

    def test_repository_update_and_cancel_return_without_followup_query(self):
        for operation in ("update", "cancel"):
            with self.subTest(operation=operation):
                db = MagicMock()
                db.execute.return_value.scalar_one_or_none.return_value = reservation_value()
                repository = ReservationRepository()
                with patch.object(
                    repository,
                    "get_by_id",
                    side_effect=AssertionError("post-commit query is forbidden"),
                ):
                    if operation == "update":
                        result = repository.update_reservation_field(
                            db, 17, "people", 5, uuid4()
                        )
                    else:
                        result = repository.cancel_reservation(db, 17, uuid4())
                self.assertEqual(result.id, 17)
                db.commit.assert_not_called()
                db.rollback.assert_not_called()
                db.refresh.assert_not_called()

    def test_service_commits_once_and_returns_immutable_dto(self):
        repository = MagicMock()
        repository.create.return_value = reservation_value()
        service = ReservationService(repository=repository)
        db = SpySession()
        result = service.create_reservation(
            db,
            ReservationCreate(
                name="Rizal",
                people=4,
                date="2026-08-01",
                time="19:00",
            ),
            owner_customer_id=uuid4(),
        )
        self.assertIsInstance(result, PersistedReservationDTO)
        self.assertEqual(result.id, 17)
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        with self.assertRaises(Exception):
            result.people = 8

    def test_invalid_input_never_enters_repository_or_transaction(self):
        repository = MagicMock()
        service = ReservationService(repository=repository)
        db = SpySession()
        with self.assertRaises(Exception):
            service.update_reservation_field(
                db,
                17,
                "people",
                0,
                owner_customer_id=uuid4(),
            )
        repository.update_reservation_field.assert_not_called()
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 0)

    def test_precommit_repository_failure_rolls_back_once(self):
        repository = MagicMock()
        repository.create.side_effect = RuntimeError("private insert failure")
        service = ReservationService(repository=repository)
        db = SpySession()
        with self.assertRaises(PersistenceOperationError):
            service.create_reservation(
                db,
                ReservationCreate(
                    name="Rizal",
                    people=4,
                    date="2026-08-01",
                    time="19:00",
                ),
                owner_customer_id=uuid4(),
            )
        self.assertEqual(db.rollbacks, 1)


class ChatReservationTransactionTests(unittest.TestCase):
    def test_conversational_create_uses_ingress_session_and_persisted_id(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        owner = uuid4()
        db = object()
        memory.update_session(
            "owner:session",
            {
                "name": "Rizal",
                "people": 4,
                "date": "2026-08-01",
                "time": "19:00",
                "awaiting_confirmation": True,
            },
        )
        agent.reservation_service.create_reservation = MagicMock(
            return_value=reservation_dto(identifier=321)
        )
        result = asyncio.run(
            agent.handle_confirmation(
                "Ya",
                "owner:session",
                owner_customer_id=owner,
                db=db,
            )
        )
        self.assertIn("321", result["response"])
        self.assertEqual(memory.get_session("owner:session")["reservation_id"], 321)
        self.assertIs(
            agent.reservation_service.create_reservation.call_args.args[0],
            db,
        )

    def test_workflow_passes_ingress_session_to_reservation_agent(self):
        workflow = AgentWorkflow()
        reservation_agent = workflow._agents["reservation"]
        reservation_agent.run = AsyncMock(return_value={"status": "ok", "response": "ok"})
        db = object()
        asyncio.run(
            workflow.execute(
                {"intent": "reservation", "steps": [{"action": "save_reservation"}]},
                {},
                "pesan",
                session_id="owner:session",
                owner_customer_id=uuid4(),
                db=db,
            )
        )
        self.assertIs(reservation_agent.run.call_args.kwargs["db"], db)


class TicketAndOwnerTransactionTests(unittest.TestCase):
    @staticmethod
    def handoff_state():
        return {
            "category": "explicit_human_request",
            "reason_code": "explicit_human_request",
            "priority": "high",
            "attempt_count": 1,
        }

    def test_ticket_and_outbox_stage_before_one_commit(self):
        events = []
        ticket = SimpleNamespace(
            id=9,
            ticket_number="CS-2026-000009",
            category="explicit_human_request",
            reason_code="explicit_human_request",
            priority="high",
            status="open",
            attempt_count=1,
            created_at=None,
        )
        repository = MagicMock(spec=SupportTicketRepository)
        repository.get_active_by_owner_and_session_hash.return_value = None
        repository.create.side_effect = lambda *_args, **_kwargs: (
            events.append("ticket") or ticket
        )

        class Outbox:
            def enqueue_new_ticket(self, _db, *, ticket):
                events.append("outbox")
                return ticket

        db = SpySession()
        original_commit = db.commit

        def commit():
            events.append("commit")
            original_commit()

        db.commit = commit
        result = TicketService(repository, Outbox()).create_or_get(
            db,
            owner_customer_id=uuid4(),
            memory_key="owner:session",
            handoff_state=self.handoff_state(),
        )
        self.assertEqual(events, ["ticket", "outbox", "commit"])
        self.assertEqual(result.ticket_number, ticket.ticket_number)
        self.assertEqual(db.commits, 1)

    def test_outbox_failure_rolls_back_ticket_transaction(self):
        repository = MagicMock(spec=SupportTicketRepository)
        repository.get_active_by_owner_and_session_hash.return_value = None
        repository.create.return_value = SimpleNamespace(
            id=8,
            ticket_number="CS-2026-000008",
        )

        class FailingOutbox:
            def enqueue_new_ticket(self, _db, *, ticket):
                raise RuntimeError("private outbox failure")

        db = SpySession()
        with self.assertRaises(PersistenceOperationError):
            TicketService(repository, FailingOutbox()).create_or_get(
                db,
                owner_customer_id=uuid4(),
                memory_key="owner:session",
                handoff_state=self.handoff_state(),
            )
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.commits, 0)

    def test_ticket_repositories_never_commit_or_rollback(self):
        ticket_db = MagicMock()
        ticket_db.flush.side_effect = RuntimeError("private flush failure")
        with self.assertRaises(RuntimeError):
            SupportTicketRepository().create(
                ticket_db,
                owner_customer_id=uuid4(),
                session_reference_hash="a" * 64,
                category="explicit_human_request",
                reason_code="explicit_human_request",
                priority="high",
                attempt_count=1,
            )
        ticket_db.commit.assert_not_called()
        ticket_db.rollback.assert_not_called()

        notification_db = MagicMock()
        notification_db.execute.side_effect = RuntimeError("private query failure")
        with self.assertRaises(RuntimeError):
            SupportTicketNotificationRepository().claim_due(
                notification_db,
                lease_seconds=60,
            )
        notification_db.commit.assert_not_called()
        notification_db.rollback.assert_not_called()

    def test_owner_transition_commits_once(self):
        now = SimpleNamespace()
        ticket = SimpleNamespace(
            ticket_number="CS-2026-000001",
            category="explicit_human_request",
            priority="high",
            status="open",
            created_at=now,
            updated_at=now,
            resolved_at=None,
        )
        repository = MagicMock(spec=SupportTicketRepository)
        repository.get_for_owner_transition.return_value = ticket
        db = SpySession()
        result = OwnerTicketService(repository).take_ticket(
            db,
            ticket.ticket_number,
        )
        self.assertEqual(result.code, "success")
        self.assertEqual(ticket.status, "in_progress")
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)


class IdentityAndIngressTests(unittest.TestCase):
    def test_identity_conflict_retries_only_after_rollback(self):
        owner = uuid4()
        identity = SimpleNamespace(
            is_active=True,
            customer_id=owner,
        )

        class Repository:
            def __init__(self):
                self.lookups = 0

            def get_by_user_key(self, db, _key):
                self.lookups += 1
                return None if self.lookups == 1 else identity

            def add(self, db, **_kwargs):
                raise IntegrityError("private statement", {}, RuntimeError("private"))

        class Session(SpySession):
            def add(self, value):
                if getattr(value, "id", None) is None:
                    value.id = uuid4()
                super().add(value)

            def get(self, _model, identifier):
                self.transaction_active = True
                return SimpleNamespace(
                    id=identifier,
                    token_version=1,
                    is_active=True,
                )

        db = Session()
        context = TelegramIdentityService(Repository()).resolve_or_create(
            db,
            telegram_user_id=1,
            identity_secret="x" * 32,
        )
        self.assertIsInstance(context, AuthenticatedCustomer)
        self.assertEqual(context.id, owner)
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.commits, 1)

    def test_send_failure_does_not_rollback_committed_business_state(self):
        db = SpySession()
        customer = SimpleNamespace(id=uuid4())
        identity = SimpleNamespace(resolve_or_create=lambda *_args, **_kwargs: customer)
        chat = SimpleNamespace(
            process=AsyncMock(return_value="committed response"),
        )
        message = SimpleNamespace(
            text="Halo",
            reply_text=AsyncMock(side_effect=RuntimeError("private send error")),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        handlers = TelegramCustomerHandlers(
            identity_secret="x" * 32,
            session_factory=lambda: db,
            identity_service=identity,
            chat_service=chat,
        )
        asyncio.run(handlers.text_message(update, None))
        self.assertEqual(db.rollbacks, 0)
        self.assertEqual(db.closes, 1)

    def test_handoff_read_transaction_is_closed_before_provider_await(self):
        owner = uuid4()
        db = SpySession()

        class EmptyTicketRepository:
            def get_active_by_owner_and_session_hash(self, _db, _owner, _hash):
                db.transaction_active = True
                return None

        ticket_service = TicketService(EmptyTicketRepository())

        class Handoff:
            def restore_active_handoff(self, memory_key, session, owner_customer_id):
                return ticket_service.get_active(
                    session,
                    owner_customer_id=owner_customer_id,
                    memory_key=memory_key,
                )

            @staticmethod
            def recovery_error_response():
                return "recovery error"

        class Agent:
            handoff_service = Handoff()

            async def handle(self, **_kwargs):
                self.transaction_seen_during_await = db.transaction_active
                await asyncio.sleep(0)
                return "ok"

        agent = Agent()
        result = asyncio.run(
            AuthenticatedChatService(agent=agent).process(
                db=db,
                customer=SimpleNamespace(id=owner),
                session_reference="chat-01",
                message="Halo",
            )
        )
        self.assertEqual(result, "ok")
        self.assertFalse(agent.transaction_seen_during_await)
        self.assertEqual(db.commits, 1)


if __name__ == "__main__":
    unittest.main()
