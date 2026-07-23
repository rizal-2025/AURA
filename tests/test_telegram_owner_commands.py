"""Offline coverage for secure Phase F Telegram owner ticket management."""

import asyncio
import io
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.agents.orchestrator import AgentOrchestrator
from app.brain.memory_manager import MemoryManager
from app.brain.classifier import IntentClassifier
from app.db.models.customer import Customer
from app.db.models.support_ticket import SAFE_TICKET_SUMMARIES, SupportTicket
from app.db.models.support_ticket_notification import SupportTicketNotification
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.owner_authorization import authorize_owner_update
from app.integrations.telegram.owner_command_handlers import (
    TelegramOwnerCommandHandlers,
    unknown_command,
)
from app.integrations.telegram.owner_command_renderer import render_owner_command
from app.integrations.telegram.runner import (
    TelegramRunnerConfigurationError,
    build_application,
    validate_runner_configuration,
)
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.handoff.owner_ticket_service import (
    OwnerTicketDTO,
    OwnerTicketResult,
    OwnerTicketService,
)
from app.services.handoff.service import HandoffService
from app.services.handoff.ticket_service import TicketService


OWNER_ID = 246810
VALID_TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
IDENTITY_SECRET = "telegram-identity-secret-that-is-long-enough"


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.sender_chat = None
        self.forward_origin = None

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def update(*, user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="private", text="/tickets"):
    return SimpleNamespace(
        effective_message=FakeMessage(text),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=user_id),
    )


def runner_config(**overrides):
    values = {
        "TELEGRAM_BOT_TOKEN": VALID_TOKEN,
        "TELEGRAM_IDENTITY_SECRET": IDENTITY_SECRET,
        "TELEGRAM_CLEAR_WEBHOOK_ON_START": False,
        "TELEGRAM_DROP_PENDING_UPDATES": False,
        "TELEGRAM_POLL_TIMEOUT_SECONDS": 30,
        "TELEGRAM_OWNER_NOTIFICATIONS_ENABLED": False,
        "TELEGRAM_OWNER_COMMANDS_ENABLED": False,
        "TELEGRAM_OWNER_CHAT_ID": None,
        "TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS": 5,
        "TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS": 5,
        "TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS": 10,
        "TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class OwnerAuthorizationTests(unittest.TestCase):
    def test_configured_owner_is_allowed(self):
        self.assertIsNotNone(authorize_owner_update(update(), OWNER_ID))

    def test_non_owner_mismatch_and_non_private_contexts_are_denied(self):
        cases = [
            update(user_id=7, chat_id=7),
            update(user_id=OWNER_ID, chat_id=7),
            update(chat_type="group"),
            update(chat_type="supergroup"),
            update(chat_type="channel"),
        ]
        for candidate in cases:
            with self.subTest(chat_type=getattr(candidate.effective_chat, "type", None)):
                self.assertIsNone(authorize_owner_update(candidate, OWNER_ID))

    def test_sender_forwarded_and_missing_objects_fail_closed(self):
        for attribute in ("sender_chat", "forward_origin", "forward_from_chat"):
            candidate = update()
            setattr(candidate.effective_message, attribute, object())
            with self.subTest(attribute=attribute):
                self.assertIsNone(authorize_owner_update(candidate, OWNER_ID))
        for candidate in (
            None,
            SimpleNamespace(effective_message=None, effective_chat=None, effective_user=None),
            SimpleNamespace(effective_message=FakeMessage(), effective_chat=None, effective_user=SimpleNamespace(id=OWNER_ID)),
            SimpleNamespace(effective_message=FakeMessage(), effective_chat=SimpleNamespace(id=OWNER_ID, type="private"), effective_user=None),
        ):
            self.assertIsNone(authorize_owner_update(candidate, OWNER_ID))

    def test_denied_handler_responds_generically_before_database_access(self):
        calls = []
        handlers = TelegramOwnerCommandHandlers(
            owner_chat_id=OWNER_ID,
            session_factory=lambda: calls.append("db"),
        )
        candidate = update(user_id=99, chat_id=99, text="/ticket CS-2026-000001")
        asyncio.run(handlers.ticket(candidate, SimpleNamespace(args=["CS-2026-000001"])))
        self.assertEqual(calls, [])
        self.assertEqual(candidate.effective_message.replies[0][0], "Perintah tidak tersedia.")

    def test_owner_identifier_is_absent_from_logs_errors_and_response(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("AURA")
        logger.addHandler(handler)
        try:
            candidate = update(user_id=99, chat_id=99)
            owner_handlers = TelegramOwnerCommandHandlers(
                owner_chat_id=OWNER_ID,
                session_factory=lambda: self.fail("database must not open"),
            )
            asyncio.run(owner_handlers.tickets(candidate, SimpleNamespace(args=[])))
            combined = stream.getvalue() + str(candidate.effective_message.replies)
        finally:
            logger.removeHandler(handler)
        self.assertNotIn(str(OWNER_ID), combined)


class OwnerRoutingAndConfigurationTests(unittest.TestCase):
    def test_commands_are_disabled_by_default_and_validate_independently(self):
        disabled = validate_runner_configuration(runner_config())
        self.assertFalse(disabled.owner_commands_enabled)
        self.assertIsNone(disabled.owner_chat_id)

        enabled = validate_runner_configuration(runner_config(
            TELEGRAM_OWNER_COMMANDS_ENABLED="true",
            TELEGRAM_OWNER_CHAT_ID=str(OWNER_ID),
        ))
        self.assertTrue(enabled.owner_commands_enabled)
        self.assertFalse(enabled.owner_notifications_enabled)
        self.assertEqual(enabled.owner_chat_id, OWNER_ID)

        for bad in (None, True, 0, -1, "1.0", "secret-owner-value"):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(TelegramRunnerConfigurationError) as captured:
                    validate_runner_configuration(runner_config(
                        TELEGRAM_OWNER_COMMANDS_ENABLED=True,
                        TELEGRAM_OWNER_CHAT_ID=bad,
                    ))
                self.assertNotIn(str(bad), str(captured.exception))

    def test_registration_order_places_owner_and_unknown_before_customer_handlers(self):
        application = build_application(runner_config(
            TELEGRAM_OWNER_COMMANDS_ENABLED=True,
            TELEGRAM_OWNER_CHAT_ID=OWNER_ID,
        ))
        callbacks = [handler.callback.__name__ for handler in application.handlers[0]]
        self.assertEqual(
            callbacks,
            ["start", "help", "status", "tickets", "ticket", "take", "resolve", "unknown_command", "text_message", "non_text_message"],
        )

    def test_unknown_command_has_no_database_identity_or_ai_dependency(self):
        candidate = update(text="/unsupported private-data")
        asyncio.run(unknown_command(candidate, SimpleNamespace(args=["private-data"])))
        self.assertEqual(candidate.effective_message.replies, [("Perintah tidak tersedia.", {})])

    def test_malformed_owner_argument_is_rejected_before_database_access(self):
        candidate = update(text="/take private-value")
        handlers = TelegramOwnerCommandHandlers(
            owner_chat_id=OWNER_ID,
            session_factory=lambda: self.fail("database must not open"),
        )
        asyncio.run(handlers.take(candidate, SimpleNamespace(args=["private-value"])))
        self.assertEqual(candidate.effective_message.replies[0][0], "Gunakan /take <nomor_tiket>.")

    def test_explicit_owner_command_uses_ticket_service_only(self):
        candidate = update(text="/tickets")
        db = SimpleNamespace(close=lambda: None, rollback=lambda: None)
        service = SimpleNamespace(
            list_active_tickets=lambda _db, limit: OwnerTicketResult("empty")
        )
        handlers = TelegramOwnerCommandHandlers(
            owner_chat_id=OWNER_ID,
            session_factory=lambda: db,
            ticket_service=service,
        )
        asyncio.run(handlers.tickets(candidate, SimpleNamespace(args=[])))
        self.assertEqual(candidate.effective_message.replies[0][0], "Tidak ada tiket bantuan aktif.")

    def test_tickets_uses_phase_d_sender_with_awaited_string_chunks(self):
        now = datetime.now(timezone.utc)
        ticket = OwnerTicketDTO(
            "CS-2026-000001", "explicit_human_request", "high", "open", now, now
        )
        message = SimpleNamespace(reply_text=AsyncMock())
        candidate = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=OWNER_ID, type="private"),
            effective_user=SimpleNamespace(id=OWNER_ID),
        )
        db = SimpleNamespace(close=lambda: None, rollback=lambda: None)
        service = SimpleNamespace(
            list_active_tickets=lambda _db, limit: OwnerTicketResult("success", tickets=(ticket,))
        )
        handlers = TelegramOwnerCommandHandlers(
            owner_chat_id=OWNER_ID,
            session_factory=lambda: db,
            ticket_service=service,
        )
        asyncio.run(handlers.tickets(candidate, SimpleNamespace(args=[])))

        self.assertGreater(message.reply_text.await_count, 0)
        for call in message.reply_text.await_args_list:
            self.assertEqual(call.kwargs, {})
            self.assertEqual(len(call.args), 1)
            self.assertIsInstance(call.args[0], str)
            self.assertLessEqual(len(call.args[0]), 4096)

    def test_owner_send_failure_logs_only_safe_result(self):
        message = SimpleNamespace(reply_text=AsyncMock(side_effect=RuntimeError("private telegram error")))
        candidate = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=OWNER_ID, type="private"),
            effective_user=SimpleNamespace(id=OWNER_ID),
        )
        db = SimpleNamespace(close=lambda: None, rollback=lambda: None)
        service = SimpleNamespace(list_active_tickets=lambda _db, limit: OwnerTicketResult("empty"))
        handlers = TelegramOwnerCommandHandlers(
            owner_chat_id=OWNER_ID,
            session_factory=lambda: db,
            ticket_service=service,
        )
        stream = io.StringIO()
        log_handler = logging.StreamHandler(stream)
        aura_logger = logging.getLogger("AURA")
        aura_logger.addHandler(log_handler)
        try:
            asyncio.run(handlers.tickets(candidate, SimpleNamespace(args=[])))
        finally:
            aura_logger.removeHandler(log_handler)
        output = stream.getvalue()
        self.assertEqual(message.reply_text.await_count, 1)
        self.assertIn("command=tickets result=send_error", output)
        self.assertNotIn("private telegram error", output)

    def test_ordinary_owner_text_still_uses_customer_flow(self):
        db = SimpleNamespace(close=lambda: None, rollback=lambda: None)
        identity = SimpleNamespace(resolve_or_create=lambda *_args, **_kwargs: SimpleNamespace(id=uuid4()))
        chat = SimpleNamespace(process=AsyncMock(return_value="customer reply"))
        handlers = TelegramCustomerHandlers(
            identity_secret=IDENTITY_SECRET,
            session_factory=lambda: db,
            identity_service=identity,
            chat_service=chat,
        )
        candidate = update(text="halo")
        asyncio.run(handlers.text_message(candidate, SimpleNamespace()))
        chat.process.assert_awaited_once()
        self.assertEqual(candidate.effective_message.replies[0][0], "customer reply")


class OwnerTicketServiceAndRendererTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Customer.__table__.create(self.engine)
        SupportTicket.__table__.create(self.engine)
        SupportTicketNotification.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.customer_id = uuid4()
        with self.Session.begin() as db:
            db.add(Customer(id=self.customer_id))

    def tearDown(self):
        self.engine.dispose()

    def add_ticket(self, number, status="open", created_at=None, category="explicit_human_request", priority="high", session_hash=None):
        created_at = created_at or datetime.now(timezone.utc)
        with self.Session.begin() as db:
            db.add(SupportTicket(
                ticket_number=number,
                owner_customer_id=self.customer_id,
                session_reference_hash=session_hash or (number * 4)[:64].ljust(64, "a"),
                category=category,
                reason_code="explicit_human_request",
                priority=priority,
                safe_summary=SAFE_TICKET_SUMMARIES["explicit_human_request"],
                status=status,
                attempt_count=1,
                created_at=created_at,
                updated_at=created_at,
                resolved_at=created_at if status in {"resolved", "closed"} else None,
            ))

    def test_list_is_active_only_oldest_first_limited_and_safe(self):
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(12):
            self.add_ticket(f"CS-2026-{index + 1:06d}", created_at=origin + timedelta(minutes=index))
        self.add_ticket("CS-2026-000099", status="resolved", created_at=origin - timedelta(days=1))
        db = self.Session()
        try:
            result = OwnerTicketService().list_active_tickets(db)
            self.assertEqual(result.code, "success")
            self.assertEqual(len(result.tickets), 10)
            self.assertEqual(result.tickets[0].ticket_number, "CS-2026-000001")
            self.assertEqual(result.tickets[-1].ticket_number, "CS-2026-000010")
            self.assertFalse(hasattr(result.tickets[0], "owner_customer_id"))
            self.assertFalse(hasattr(result.tickets[0], "id"))
        finally:
            db.close()

    def test_detail_supports_terminal_and_renderer_uses_safe_fallbacks(self):
        self.add_ticket("CS-2026-000001", status="resolved")
        db = self.Session()
        try:
            result = OwnerTicketService().get_ticket(db, "CS-2026-000001")
        finally:
            db.close()
        message = "".join(render_owner_command("ticket", result))
        self.assertIn("Status: Selesai", message)
        self.assertNotIn(str(self.customer_id), message)

        now = datetime.now(timezone.utc)
        unsafe = OwnerTicketResult("success", ticket=OwnerTicketDTO(
            ticket_number="CS-2026-000002", category="private category",
            priority="private priority", status="private status",
            created_at=now, updated_at=now,
        ))
        fallback = "".join(render_owner_command("ticket", unsafe))
        self.assertIn("Kategori bantuan", fallback)
        self.assertIn("Prioritas tidak tersedia", fallback)
        self.assertIn("Status tidak tersedia", fallback)
        self.assertNotIn("private", fallback)

    def test_transitions_are_idempotent_terminal_safe_and_do_not_enqueue_outbox(self):
        self.add_ticket("CS-2026-000001")
        self.add_ticket("CS-2026-000002")
        self.add_ticket("CS-2026-000003", status="closed")
        service = OwnerTicketService()
        db = self.Session()
        try:
            taken = service.take_ticket(db, "CS-2026-000001")
            first_updated = taken.ticket.updated_at
            repeated_take = service.take_ticket(db, "CS-2026-000001")
            resolved = service.resolve_ticket(db, "CS-2026-000001")
            repeated_resolve = service.resolve_ticket(db, "CS-2026-000001")
            direct_resolve = service.resolve_ticket(db, "CS-2026-000002")
            closed = service.take_ticket(db, "CS-2026-000003")
            self.assertEqual(taken.code, "success")
            self.assertEqual(repeated_take.code, "already_in_progress")
            repeated_timestamp = repeated_take.ticket.updated_at
            if repeated_timestamp.tzinfo is None:
                repeated_timestamp = repeated_timestamp.replace(tzinfo=timezone.utc)
            self.assertEqual(repeated_timestamp, first_updated)
            self.assertEqual(resolved.code, "success")
            self.assertEqual(repeated_resolve.code, "already_resolved")
            self.assertEqual(direct_resolve.code, "success")
            self.assertEqual(closed.code, "closed")
            self.assertEqual(db.scalar(select(func.count()).select_from(SupportTicketNotification)), 0)
        finally:
            db.close()

    def test_malformed_unknown_and_database_failure_are_safe_and_session_reusable(self):
        service = OwnerTicketService()
        db = self.Session()
        try:
            self.assertEqual(service.get_ticket(db, "bad private argument").code, "invalid_argument")
            self.assertEqual(service.get_ticket(db, "CS-2026-999999").code, "not_available")
            self.assertEqual(db.scalar(select(func.count()).select_from(Customer)), 1)
        finally:
            db.close()

        class FailingRepository:
            def get_for_owner_transition(self, db, *, ticket_number):
                raise RuntimeError("private database details")
        fake_db = SimpleNamespace(rollbacks=0)
        fake_db.rollback = lambda: setattr(fake_db, "rollbacks", fake_db.rollbacks + 1)
        result = OwnerTicketService(FailingRepository()).take_ticket(fake_db, "CS-2026-000001")
        self.assertEqual(result.code, "database_error")
        self.assertEqual(fake_db.rollbacks, 1)

    def test_renderer_chunks_are_plain_unicode_and_bounded(self):
        now = datetime.now(timezone.utc)
        tickets = tuple(OwnerTicketDTO(
            f"CS-2026-{index:06d}", "explicit_human_request", "high", "open", now, now
        ) for index in range(1, 11))
        chunks = render_owner_command("tickets", OwnerTicketResult("success", tickets=tickets))
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        self.assertIn("Permintaan bantuan petugas", "".join(chunks))
        self.assertTrue(all("parse_mode" not in chunk for chunk in chunks))

    def test_resolved_handoff_then_greeting_creates_no_ticket_or_notification(self):
        memory = MemoryManager()
        owner = self.customer_id
        session_reference = "resolved-greeting"
        memory_key = f"{owner}:{session_reference}"
        self.add_ticket(
            "CS-2026-000090",
            status="resolved",
            session_hash=TicketService.hash_session_reference(memory_key),
        )
        memory.get_session(memory_key).update({
            "handoff_required": True,
            "handoff_state": {"status": "open", "ticket_number": "CS-2026-000090"},
        })
        failing_provider = type("FailingProvider", (), {
            "chat": AsyncMock(side_effect=ConnectionError("private provider failure")),
        })()
        orchestrator = AgentOrchestrator()
        handoff = HandoffService(memory)
        orchestrator.memory_manager = memory
        orchestrator.handoff_service = handoff
        orchestrator.intent_classifier = IntentClassifier(provider=failing_provider)
        orchestrator.ai = failing_provider

        db = self.Session()
        try:
            response = asyncio.run(AuthenticatedChatService(orchestrator).process(
                db=db,
                customer=SimpleNamespace(id=owner),
                session_reference=session_reference,
                message="Halo!",
            ))
            tickets = db.scalar(select(func.count()).select_from(SupportTicket))
            notifications = db.scalar(select(func.count()).select_from(SupportTicketNotification))
        finally:
            db.close()

        self.assertIn("Halo! Saya AURA", response)
        failing_provider.chat.assert_not_awaited()
        self.assertFalse(memory.get_session(memory_key).get("handoff_required"))
        self.assertEqual(tickets, 1)
        self.assertEqual(notifications, 0)


class StaleHandoffLockTests(unittest.TestCase):
    def ticket(self, status="open"):
        return SimpleNamespace(
            id=1, ticket_number="CS-2026-000001", category="explicit_human_request",
            reason_code="explicit_human_request", priority="high", status=status,
            attempt_count=1, created_at=datetime.now(timezone.utc),
        )

    def test_active_ticket_refreshes_lock_and_missing_active_clears_only_handoff(self):
        persistent = {"ticket": self.ticket()}
        ticket_service = SimpleNamespace(
            get_active=lambda *_args, **_kwargs: persistent["ticket"]
        )
        memory = MemoryManager()
        handoff = HandoffService(memory, ticket_service=ticket_service)
        key = "owner:session"
        session = memory.get_session(key)
        session.update({"handoff_required": True, "handoff_state": {"status": "open"}, "update_reservation_stage": "input_value", "editing_field": "people"})
        self.assertIsNotNone(handoff.restore_active_handoff(key, object(), "owner"))
        persistent["ticket"] = None
        self.assertIsNone(handoff.restore_active_handoff(key, object(), "owner"))
        state = memory.get_session(key)
        self.assertFalse(state.get("handoff_required"))
        self.assertNotIn("handoff_state", state)
        self.assertEqual(state["update_reservation_stage"], "input_value")
        self.assertEqual(state["editing_field"], "people")

    def test_same_message_continues_after_persistent_resolution(self):
        memory = MemoryManager()
        handoff = HandoffService(memory, ticket_service=SimpleNamespace(
            get_active=lambda *_args, **_kwargs: None
        ))

        class Agent:
            def __init__(self):
                self.handoff_service = handoff
                self.calls = []
            async def handle(self, **kwargs):
                self.calls.append(kwargs)
                return "continued"

        agent = Agent()
        owner = uuid4()
        key = f"{owner}:same-session"
        memory.get_session(key).update({"handoff_required": True, "handoff_state": {"status": "open"}})
        response = asyncio.run(AuthenticatedChatService(agent).process(
            db=object(), customer=SimpleNamespace(id=owner),
            session_reference="same-session", message="lihat reservasi saya",
        ))
        self.assertEqual(response, "continued")
        self.assertEqual(agent.calls[0]["message"], "lihat reservasi saya")
        self.assertFalse(memory.get_session(key).get("handoff_required"))

    def test_resolution_revalidation_is_customer_and_session_scoped(self):
        memory = MemoryManager()
        active_key = "owner-b:shared"
        tickets = {"owner-a:shared": None, active_key: self.ticket()}
        handoff = HandoffService(memory, ticket_service=SimpleNamespace(
            get_active=lambda _db, *, owner_customer_id, memory_key: tickets[memory_key]
        ))
        for key in tickets:
            memory.get_session(key).update({"handoff_required": True, "handoff_state": {"status": "open"}})
        handoff.restore_active_handoff("owner-a:shared", object(), "owner-a")
        handoff.restore_active_handoff(active_key, object(), "owner-b")
        self.assertFalse(memory.get_session("owner-a:shared").get("handoff_required"))
        self.assertTrue(memory.get_session(active_key)["handoff_required"])


if __name__ == "__main__":
    unittest.main()
