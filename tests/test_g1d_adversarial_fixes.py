import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.orchestrator import AgentOrchestrator
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.agents.view_reservation_agent import ViewReservationAgent
from app.api.error_handlers import transaction_exception_handler
from app.api.dependencies import get_current_customer
from app.brain.memory_manager import MemoryManager
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.core.unit_of_work import UnitOfWork
from app.db.database import get_db
from app.main import app
from app.integrations.telegram.handlers import (
    PERSISTENCE_UNAVAILABLE_REPLY,
    TelegramCustomerHandlers,
)
from app.integrations.telegram.owner_notification_dispatcher import (
    OwnerNotificationDispatcher,
)
from app.services.handoff.notification_outbox_service import (
    NotificationOutboxService,
)
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.reservation.dto import (
    PersistedReservationDTO,
    ReservationSelectionPage,
)
from app.services.reservation.service import ReservationService


TRANSACTION_ERRORS = (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)


class TransactionSpySession:
    def __init__(self, *, rollback_error=None):
        self.rollback_error = rollback_error
        self.transaction_active = False
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def commit(self):
        self.commits += 1
        self.transaction_active = False

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error
        self.transaction_active = False

    def close(self):
        self.closes += 1

    def in_transaction(self):
        return self.transaction_active


class ChatTransactionPropagationTests(unittest.TestCase):
    @staticmethod
    def _orchestrator():
        with patch("app.agents.orchestrator.get_ai_provider", return_value=MagicMock()):
            orchestrator = AgentOrchestrator()
        orchestrator._create_handoff = MagicMock()
        return orchestrator

    def _assert_operation_propagates(self, operation, error_type):
        orchestrator = self._orchestrator()
        key = "owner:conversation"
        owner = uuid4()
        session = orchestrator.memory_manager.get_session(key)

        if operation == "create":
            session.update(
                {
                    "intent": "reservation",
                    "intent_confidence": 1.0,
                    "awaiting_confirmation": True,
                }
            )
            orchestrator.workflow.execute = AsyncMock(side_effect=error_type())
        elif operation == "update":
            session["update_reservation_stage"] = "select_reservation_reference"
            orchestrator.update_reservation_agent.run = AsyncMock(
                side_effect=error_type()
            )
        else:
            session["cancel_reservation_stage"] = "select_reservation_reference"
            orchestrator.cancel_reservation_agent.run = AsyncMock(
                side_effect=error_type()
            )

        with self.assertRaises(error_type):
            asyncio.run(
                orchestrator.handle(
                    session_id=key,
                    message="Ya" if operation == "create" else "1",
                    db=object(),
                    owner_customer_id=owner,
                )
            )
        orchestrator._create_handoff.assert_not_called()
        state = orchestrator.memory_manager.get_session(key)
        self.assertFalse(state.get("handoff_required", False))
        self.assertIsNone(state.get("handoff_category"))

    def test_create_update_cancel_propagate_all_transaction_errors(self):
        for operation in ("create", "update", "cancel"):
            for error_type in TRANSACTION_ERRORS:
                with self.subTest(operation=operation, error=error_type.__name__):
                    self._assert_operation_propagates(operation, error_type)

    def test_http_mapping_is_safe_for_each_transaction_error(self):
        expected = {
            PersistenceOperationError: "PERSISTENCE_OPERATION_FAILED",
            PersistenceOutcomeUnknownError: "PERSISTENCE_OUTCOME_UNKNOWN",
            TransactionSessionUnusableError: "PERSISTENCE_SESSION_UNAVAILABLE",
        }
        for error_type, code in expected.items():
            with self.subTest(error=error_type.__name__):
                response = asyncio.run(transaction_exception_handler(None, error_type()))
                self.assertEqual(response.status_code, 503)
                self.assertIn(code, response.body.decode("utf-8"))

    def test_http_chat_reaches_registered_transaction_handlers(self):
        expected = {
            PersistenceOperationError: "PERSISTENCE_OPERATION_FAILED",
            PersistenceOutcomeUnknownError: "PERSISTENCE_OUTCOME_UNKNOWN",
            TransactionSessionUnusableError: "PERSISTENCE_SESSION_UNAVAILABLE",
        }

        def override_db():
            yield object()

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(
            id=uuid4()
        )
        try:
            client = TestClient(app)
            for error_type, code in expected.items():
                with self.subTest(error=error_type.__name__), patch(
                    "app.api.chat.authenticated_chat_service.process",
                    AsyncMock(side_effect=error_type()),
                ):
                    response = client.post(
                        "/chat",
                        json={"session_id": "chat-01", "message": "Halo"},
                    )
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["code"], code)
        finally:
            app.dependency_overrides.clear()

    def test_http_mapping_accepts_future_subclasses(self):
        class FutureOperationError(PersistenceOperationError):
            pass

        response = asyncio.run(
            transaction_exception_handler(None, FutureOperationError())
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("PERSISTENCE_OPERATION_FAILED", response.body.decode("utf-8"))

    def test_telegram_maps_each_transaction_error_without_exposing_details(self):
        for error_type in TRANSACTION_ERRORS:
            with self.subTest(error=error_type.__name__):
                db = TransactionSpySession()
                customer = SimpleNamespace(id=uuid4())
                identity = SimpleNamespace(
                    resolve_or_create=lambda *_args, **_kwargs: customer
                )
                chat = SimpleNamespace(process=AsyncMock(side_effect=error_type()))
                reply_text = AsyncMock()
                update = SimpleNamespace(
                    effective_message=SimpleNamespace(
                        text="Halo",
                        reply_text=reply_text,
                    ),
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
                sent = " ".join(call.args[0] for call in reply_text.await_args_list)
                self.assertEqual(sent, PERSISTENCE_UNAVAILABLE_REPLY)
                self.assertLessEqual(len(sent), 4096)
                self.assertNotIn(error_type.__name__, sent)

    def test_handoff_recovery_persistence_error_also_propagates(self):
        for error_type in TRANSACTION_ERRORS:
            with self.subTest(error=error_type.__name__):
                class Handoff:
                    @staticmethod
                    def restore_active_handoff(*_args, **_kwargs):
                        raise error_type()

                agent = SimpleNamespace(
                    handoff_service=Handoff(),
                    handle=AsyncMock(),
                )
                service = AuthenticatedChatService(agent=agent)
                with self.assertRaises(error_type):
                    asyncio.run(
                        service.process(
                            db=object(),
                            customer=SimpleNamespace(id=uuid4()),
                            session_reference="chat-01",
                            message="Halo",
                        )
                    )
                agent.handle.assert_not_awaited()


class UnitOfWorkSingleUseTests(unittest.TestCase):
    def test_committed_unit_of_work_cannot_be_reentered(self):
        db = TransactionSpySession()
        unit = UnitOfWork(db)
        with unit:
            unit.commit()
        with self.assertRaises(PersistenceOperationError):
            with unit:
                self.fail("single-use UnitOfWork must not re-enter")
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        self.assertFalse(db.in_transaction())

    def test_rolled_back_unit_of_work_cannot_be_reentered(self):
        db = TransactionSpySession()
        unit = UnitOfWork(db)
        with self.assertRaises(PersistenceOperationError):
            with unit:
                raise RuntimeError("private")
        with self.assertRaises(PersistenceOperationError):
            with unit:
                self.fail("rolled-back UnitOfWork must not re-enter")
        self.assertEqual(db.rollbacks, 1)
        self.assertFalse(db.in_transaction())

    def test_explicit_commit_then_later_exception_never_rolls_back(self):
        db = TransactionSpySession()
        unit = UnitOfWork(db)
        with self.assertRaisesRegex(RuntimeError, "after commit"):
            with unit:
                unit.commit()
                raise RuntimeError("after commit")
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)


class DatabaseDependencyCleanupTests(unittest.TestCase):
    def _throw_with_rollback_failure(self, original):
        db = TransactionSpySession(rollback_error=RuntimeError("private rollback"))
        db.transaction_active = True
        with patch("app.db.database.SessionLocal", return_value=db):
            dependency = get_db()
            self.assertIs(next(dependency), db)
            with self.assertRaises(type(original)) as raised:
                dependency.throw(original)
        self.assertIs(raised.exception, original)
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.closes, 1)

    def test_authoritative_transaction_errors_survive_cleanup_failure(self):
        for error in (
            PersistenceOutcomeUnknownError(),
            PersistenceOperationError(),
        ):
            with self.subTest(error=type(error).__name__):
                self._throw_with_rollback_failure(error)

    def test_ordinary_application_error_survives_cleanup_failure(self):
        self._throw_with_rollback_failure(RuntimeError("authoritative"))

    def test_cleanup_only_failure_raises_session_unusable_and_closes(self):
        db = TransactionSpySession(rollback_error=RuntimeError("private rollback"))
        db.transaction_active = True
        with patch("app.db.database.SessionLocal", return_value=db):
            dependency = get_db()
            self.assertIs(next(dependency), db)
            with self.assertRaises(TransactionSessionUnusableError):
                next(dependency)
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.closes, 1)


class PersistedReservationDTOTests(unittest.TestCase):
    @staticmethod
    def _legacy_row():
        return SimpleNamespace(
            id=91,
            name="Legacy / Imported",
            people=25,
            date="01/08/2026",
            time="7pm",
            status="pending",
            owner_customer_id=uuid4(),
            public_reference="RSV_91919191919191919191919191919191",
        )

    def test_legacy_row_bypasses_current_input_validators_but_is_detached(self):
        row = self._legacy_row()
        repository = MagicMock()
        repository.list_recent.return_value = [row]
        repository.get_by_public_reference.return_value = row
        repository.get_active_by_public_reference.return_value = row
        repository.update_reservation_field_by_public_reference.return_value = row
        repository.cancel_reservation_by_public_reference.return_value = row
        service = ReservationService(repository)
        owner = uuid4()

        listed = service.list_recent_reservations(
            TransactionSpySession(), owner_customer_id=owner
        )[0]
        selected = service.get_reservation_by_reference(
            TransactionSpySession(),
            "RSV_91919191919191919191919191919191",
            owner_customer_id=owner,
        )
        # Legacy rows remain readable/cancellable, but every successful update
        # must now leave a temporally valid reservation (including name-only).
        with self.assertRaises(PersistenceOperationError):
            service.update_reservation_field_by_reference(
                TransactionSpySession(),
                "RSV_91919191919191919191919191919191",
                "name",
                "Nama Baru",
                owner_customer_id=owner,
            )
        repository.update_reservation_field_by_public_reference.assert_not_called()
        cancelled = service.cancel_reservation_by_reference(
            TransactionSpySession(),
            "RSV_91919191919191919191919191919191",
            owner_customer_id=owner,
        )

        for value in (listed, selected, cancelled):
            self.assertIsInstance(value, PersistedReservationDTO)
            self.assertEqual(value.people, 25)
            self.assertEqual(value.date, "01/08/2026")
            self.assertEqual(value.time, "7pm")
            self.assertFalse(hasattr(value, "owner_customer_id"))
        with self.assertRaises(Exception):
            listed.people = 2

    def test_view_update_and_cancel_selection_format_legacy_row(self):
        row = PersistedReservationDTO(
            id=91,
            name="Legacy / Imported",
            people=25,
            date="01/08/2026",
            time="7pm",
            status="pending",
            reference="RSV_91919191919191919191919191919191",
        )
        service = MagicMock()
        service.list_recent_reservations.return_value = (row,)
        service.list_selectable_reservations.return_value = (row,)
        service.list_selectable_reservation_page.return_value = (
            ReservationSelectionPage((row,), False)
        )
        owner = uuid4()

        view = asyncio.run(
            ViewReservationAgent(service).run(object(), "key", owner)
        )
        update = asyncio.run(
            UpdateReservationAgent(MemoryManager(), service).run(
                object(), "update-key", "ubah reservasi", owner
            )
        )
        cancel = asyncio.run(
            CancelReservationAgent(MemoryManager(), service).run(
                object(), "cancel-key", "batalkan reservasi", owner
            )
        )
        for result in (view, update, cancel):
            self.assertIn("25", result["response"])
            self.assertIn("01/08/2026", result["response"])
            self.assertNotIn("internal_error", result["response"])


class CancelReconciliationBoundaryTests(unittest.TestCase):
    def test_secondary_read_begins_after_zero_row_mutation_transaction_ends(self):
        observations = []
        cancelled = SimpleNamespace(
            id=12,
            name="Rizal",
            people=4,
            date="2026-08-01",
            time="19:00",
            status="cancelled",
            public_reference="RSV_12121212121212121212121212121212",
        )

        class Repository:
            def get_active_by_public_reference(self, db, *_args, **_kwargs):
                observations.append(("active", db.in_transaction()))
                db.transaction_active = True
                return SimpleNamespace(
                    **{
                        **vars(cancelled),
                        "status": "pending",
                    }
                )

            def cancel_reservation_by_public_reference(self, db, *_args, **_kwargs):
                db.transaction_active = True
                return None

            def get_by_public_reference(self, db, *_args, **_kwargs):
                observations.append(("current", db.in_transaction()))
                db.transaction_active = True
                return cancelled

        db = TransactionSpySession()
        memory = MemoryManager()
        session = memory.get_session("cancel-key")
        session.update(
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": "RSV_12121212121212121212121212121212",
            }
        )
        agent = CancelReservationAgent(
            memory,
            ReservationService(Repository()),
        )
        result = asyncio.run(agent.run(db, "cancel-key", "Ya", uuid4()))
        self.assertEqual(observations, [("active", False), ("current", False)])
        self.assertIn("sudah dibatalkan", result["response"])
        self.assertEqual(db.commits, 3)

    def test_secondary_read_persistence_failure_is_not_mapped_to_not_found(self):
        class Repository:
            def get_active_by_public_reference(self, _db, *_args, **_kwargs):
                return SimpleNamespace(
                    id=12,
                    name="Rizal",
                    people=4,
                    date="2026-08-01",
                    time="19:00",
                    status="pending",
                    public_reference="RSV_12121212121212121212121212121212",
                )

            def cancel_reservation_by_public_reference(self, db, *_args, **_kwargs):
                db.transaction_active = True
                return None

            def get_by_public_reference(self, db, *_args, **_kwargs):
                raise PersistenceOperationError()

        memory = MemoryManager()
        memory.get_session("cancel-key").update(
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": "RSV_12121212121212121212121212121212",
            }
        )
        agent = CancelReservationAgent(
            memory,
            ReservationService(Repository()),
        )
        with self.assertRaises(PersistenceOperationError):
            asyncio.run(
                agent.run(
                    TransactionSpySession(),
                    "cancel-key",
                    "Ya",
                    uuid4(),
                )
            )


class OutboxParticipantAndDispatcherTests(unittest.TestCase):
    @staticmethod
    def _notification():
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            id=7,
            support_ticket_id=8,
            channel="telegram_owner",
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
            lease_expires_at=None,
            sent_at=None,
            telegram_message_id=None,
            last_error_code=None,
            created_at=now,
            updated_at=now,
        )

    def test_public_enqueue_owns_transaction_and_private_stage_never_commits(self):
        repository = MagicMock()
        repository.add_pending.return_value = self._notification()
        service = NotificationOutboxService(repository)
        ticket = SimpleNamespace(id=8)

        public_db = TransactionSpySession()
        service.enqueue_new_ticket(public_db, ticket=ticket)
        self.assertEqual(public_db.commits, 1)

        participant_db = TransactionSpySession()
        service._stage_new_ticket(participant_db, ticket=ticket)
        self.assertEqual(participant_db.commits, 0)
        self.assertEqual(participant_db.rollbacks, 0)

        with self.assertRaises(TypeError):
            service.enqueue_new_ticket(
                TransactionSpySession(),
                ticket=ticket,
                transaction_participant=True,
            )

    @staticmethod
    def _dispatcher(outbox, bot):
        class Db:
            def get(self, _model, _identifier):
                return SimpleNamespace()

            def close(self):
                pass

        return OwnerNotificationDispatcher(
            bot=bot,
            session_factory=Db,
            owner_chat_id=1,
            config=SimpleNamespace(
                owner_notification_lease_seconds=60,
                owner_notification_max_attempts=5,
                owner_notification_retry_base_seconds=10,
                owner_notification_poll_seconds=5,
            ),
            outbox_service=outbox,
        )

    def test_mark_sent_persistence_errors_are_not_telegram_failures(self):
        for error_type in TRANSACTION_ERRORS:
            with self.subTest(error=error_type.__name__):
                outbox = MagicMock()
                outbox.claim_due.return_value = SimpleNamespace(
                    id=7,
                    support_ticket_id=8,
                )
                outbox.mark_sent.side_effect = error_type()
                bot = SimpleNamespace(
                    send_message=AsyncMock(
                        return_value=SimpleNamespace(message_id=9)
                    )
                )
                dispatcher = self._dispatcher(outbox, bot)
                with patch(
                    "app.integrations.telegram.owner_notification_dispatcher."
                    "render_owner_notification",
                    return_value=["safe"],
                ), patch(
                    "app.integrations.telegram.owner_notification_dispatcher."
                    "classify_telegram_failure",
                ) as classify:
                    self.assertTrue(asyncio.run(dispatcher.process_once()))
                classify.assert_not_called()
                outbox.mark_failed_attempt.assert_not_called()
                self.assertEqual(bot.send_message.await_count, 1)

    def test_mark_sent_failure_logs_only_stable_persistence_category(self):
        private_detail = "private-database-value"
        outbox = MagicMock()
        outbox.claim_due.return_value = SimpleNamespace(
            id=7,
            support_ticket_id=8,
        )
        try:
            raise PersistenceOperationError() from RuntimeError(private_detail)
        except PersistenceOperationError as error:
            outbox.mark_sent.side_effect = error
        dispatcher = self._dispatcher(
            outbox,
            SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=9)
                )
            ),
        )
        with patch(
            "app.integrations.telegram.owner_notification_dispatcher."
            "render_owner_notification",
            return_value=["safe"],
        ), self.assertLogs("AURA", level="ERROR") as captured:
            self.assertTrue(asyncio.run(dispatcher.process_once()))
        output = " ".join(captured.output)
        self.assertIn("code=PERSISTENCE_OPERATION_FAILED", output)
        self.assertNotIn(private_detail, output)
        self.assertNotIn("support_ticket_id", output)
        outbox.mark_failed_attempt.assert_not_called()

    def test_actual_network_failure_is_classified_and_not_resent_in_iteration(self):
        class NetworkError(Exception):
            pass

        outbox = MagicMock()
        outbox.claim_due.return_value = SimpleNamespace(
            id=7,
            support_ticket_id=8,
        )
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=NetworkError()))
        dispatcher = self._dispatcher(outbox, bot)
        with patch(
            "app.integrations.telegram.owner_notification_dispatcher."
            "render_owner_notification",
            return_value=["first", "second"],
        ):
            self.assertTrue(asyncio.run(dispatcher.process_once()))
        self.assertEqual(bot.send_message.await_count, 1)
        outbox.mark_failed_attempt.assert_called_once()
        self.assertEqual(
            outbox.mark_failed_attempt.call_args.kwargs["error_code"],
            "network_error",
        )
        outbox.mark_sent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
