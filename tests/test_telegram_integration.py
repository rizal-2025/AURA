"""Offline tests for the Phase D Telegram integration boundary."""

import asyncio
import io
import logging
import os
import re
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import httpx

from app.core.config import Settings
from app.core.logger import RedactingFormatter, configure_safe_logging, logger
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.identity import (
    derive_telegram_session_reference,
    derive_telegram_user_key,
)
from app.integrations.telegram.message_utils import split_telegram_reply
from app.integrations.telegram.runner import (
    TelegramRunnerConfigurationError,
    TelegramWebhookConflictError,
    build_application,
    prepare_polling,
    safe_ptb_error_handler,
    validate_runner_configuration,
)
from app.integrations.telegram.identity_service import (
    TelegramIdentityService,
    TelegramIdentityUnavailableError,
)
from app.db.repositories.telegram_identity_repository import TelegramIdentityRepository
from app.services.authenticated_chat_service import AuthenticatedChatService


IDENTITY_SECRET = "telegram-identity-secret-that-is-long-enough"
VALID_TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"


class FakeDb:
    def __init__(self):
        self.closed = False
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakeMessage:
    def __init__(self, text=None, failures=0):
        self.text = text
        self.replies = []
        self.failures = failures
        self.calls = 0

    async def reply_text(self, text, **kwargs):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("synthetic send failure")
        self.replies.append((text, kwargs))


class FakeIdentityService:
    def __init__(self):
        self.calls = []
        self.customers = {}

    def resolve_or_create(self, db, *, telegram_user_id, identity_secret):
        self.calls.append((telegram_user_id, identity_secret))
        return self.customers.setdefault(
            telegram_user_id,
            SimpleNamespace(id=uuid4(), is_active=True),
        )


class FakeChatService:
    def __init__(self, response="AURA siap membantu.", status_response=None):
        self.response = response
        self.status_response = status_response or "Saat ini Anda tidak memiliki tiket bantuan yang aktif."
        self.calls = []
        self.status_calls = []

    async def process(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def ticket_status(self, **kwargs):
        self.status_calls.append(kwargs)
        return self.status_response


def private_update(user_id=1001, chat_id=2001, text="halo"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
        effective_message=FakeMessage(text),
    )


class TelegramConfigurationTests(unittest.TestCase):
    def config(self, **overrides):
        values = dict(
            APP_ENV="test",
            TELEGRAM_BOT_TOKEN=VALID_TOKEN,
            TELEGRAM_IDENTITY_SECRET=IDENTITY_SECRET,
            TELEGRAM_CLEAR_WEBHOOK_ON_START=False,
            TELEGRAM_DROP_PENDING_UPDATES=False,
            TELEGRAM_POLL_TIMEOUT_SECONDS=30,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_runner_rejects_missing_token_without_leaking_secret(self):
        secret = "do-not-leak-telegram-identity-secret-12345"
        with self.assertRaises(TelegramRunnerConfigurationError) as context:
            validate_runner_configuration(self.config(TELEGRAM_BOT_TOKEN=None, TELEGRAM_IDENTITY_SECRET=secret))
        self.assertNotIn(secret, str(context.exception))

    def test_runner_rejects_short_secret(self):
        with self.assertRaises(TelegramRunnerConfigurationError):
            validate_runner_configuration(self.config(TELEGRAM_IDENTITY_SECRET="short"))

    def test_runner_rejects_whitespace_and_control_character_secrets(self):
        for value in (" " * 32, "x" * 31 + "\n", "x" * 31 + "\t", "x" * 31 + "\0"):
            with self.subTest(kind=repr(value[-1])):
                with self.assertRaises(TelegramRunnerConfigurationError) as context:
                    validate_runner_configuration(self.config(TELEGRAM_IDENTITY_SECRET=value))
                self.assertNotIn(value, str(context.exception))

    def test_runner_rejects_malformed_timeouts(self):
        for value in (True, 0, -1, 61, 1.0, "0", "-1", "1.0", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(TelegramRunnerConfigurationError):
                    validate_runner_configuration(self.config(TELEGRAM_POLL_TIMEOUT_SECONDS=value))

    def test_fastapi_settings_ignore_missing_or_malformed_telegram_values(self):
        configured = Settings(
            _env_file=None,
            APP_ENV="test",
            DATABASE_URL="sqlite://",
            AUTH_JWT_SECRET="test-fastapi-secret-not-for-production-12345",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="test-model",
            TELEGRAM_BOT_TOKEN=None,
            TELEGRAM_IDENTITY_SECRET=None,
            TELEGRAM_POLL_TIMEOUT_SECONDS="malformed",
            TELEGRAM_CLEAR_WEBHOOK_ON_START="malformed",
        )
        self.assertFalse(hasattr(configured, "TELEGRAM_BOT_TOKEN"))
        self.assertFalse(hasattr(configured, "TELEGRAM_POLL_TIMEOUT_SECONDS"))

    def test_fastapi_imports_with_malformed_optional_telegram_environment(self):
        environment = dict(os.environ)
        environment.update({
            "APP_ENV": "test",
            "DATABASE_URL": "sqlite://",
            "AUTH_JWT_SECRET": "test-fastapi-secret-not-for-production-12345",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_IDENTITY_SECRET": "",
            "TELEGRAM_POLL_TIMEOUT_SECONDS": "not-an-integer",
            "TELEGRAM_CLEAR_WEBHOOK_ON_START": "not-a-boolean",
            "TELEGRAM_OWNER_NOTIFICATIONS_ENABLED": "not-a-boolean",
            "TELEGRAM_OWNER_CHAT_ID": "not-an-integer",
            "TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS": "not-an-integer",
            "TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS": "not-an-integer",
            "TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS": "not-an-integer",
            "TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS": "not-an-integer",
        })
        result = subprocess.run(
            [sys.executable, "-c", "from app.main import app; print(app.title)"],
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runner_rejects_invalid_token(self):
        with self.assertRaises(TelegramRunnerConfigurationError):
            validate_runner_configuration(self.config(TELEGRAM_BOT_TOKEN="invalid"))

    def test_webhook_blocks_polling_unless_explicitly_enabled(self):
        app = SimpleNamespace(
            bot_data={"aura_runner_config": validate_runner_configuration(self.config())},
            bot=SimpleNamespace(),
        )
        async def active_webhook():
            return SimpleNamespace(url="https://example.invalid/hook")
        app.bot.get_webhook_info = active_webhook
        with self.assertRaises(TelegramWebhookConflictError):
            asyncio.run(prepare_polling(app))

    def test_webhook_clear_is_explicit(self):
        deleted = []
        app = SimpleNamespace(
            bot_data={"aura_runner_config": validate_runner_configuration(self.config(TELEGRAM_CLEAR_WEBHOOK_ON_START=True, TELEGRAM_DROP_PENDING_UPDATES=True))},
            bot=SimpleNamespace(),
        )
        async def active_webhook():
            return SimpleNamespace(url="https://example.invalid/hook")
        async def delete_webhook(**kwargs):
            deleted.append(kwargs)
        app.bot.get_webhook_info = active_webhook
        app.bot.delete_webhook = delete_webhook
        asyncio.run(prepare_polling(app))
        self.assertEqual(deleted, [{"drop_pending_updates": True}])

    def test_application_registers_safe_error_handler_without_network(self):
        application = build_application(self.config())
        self.assertIn(safe_ptb_error_handler, application.error_handlers)


class TelegramHandlerTests(unittest.TestCase):
    def make_handlers(self, response="AURA siap membantu."):
        self.db = FakeDb()
        self.identity = FakeIdentityService()
        self.chat = FakeChatService(response)
        return TelegramCustomerHandlers(
            identity_secret=IDENTITY_SECRET,
            session_factory=lambda: self.db,
            identity_service=self.identity,
            chat_service=self.chat,
        )

    def test_identity_and_session_hmac_are_deterministic_and_private(self):
        first_key = derive_telegram_user_key(IDENTITY_SECRET, 12345)
        self.assertEqual(first_key, derive_telegram_user_key(IDENTITY_SECRET, "12345"))
        first_session = derive_telegram_session_reference(IDENTITY_SECRET, 12345, 999)
        self.assertEqual(first_session, derive_telegram_session_reference(IDENTITY_SECRET, 12345, 999))
        self.assertRegex(first_key, r"^[0-9a-f]{64}$")
        self.assertRegex(first_session, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first_session, derive_telegram_session_reference(IDENTITY_SECRET, 12346, 999))
        self.assertNotEqual(first_key, first_session)
        self.assertNotEqual(first_key, derive_telegram_user_key("z" * 32, 12345))

    def test_start_creates_identity_without_calling_chat(self):
        handlers = self.make_handlers()
        update = private_update()
        asyncio.run(handlers.start(update, None))
        self.assertEqual(len(self.identity.calls), 1)
        self.assertEqual(self.chat.calls, [])
        self.assertIn("AURA", update.effective_message.replies[0][0])

    def test_private_text_uses_shared_authenticated_chat_service(self):
        handlers = self.make_handlers()
        update = private_update(text="lihat reservasi saya")
        asyncio.run(handlers.text_message(update, None))
        self.assertEqual(len(self.chat.calls), 1)
        call = self.chat.calls[0]
        self.assertEqual(call["message"], "lihat reservasi saya")
        self.assertEqual(call["customer"], self.identity.customers[1001])
        self.assertRegex(call["session_reference"], r"^[0-9a-f]{64}$")

    def test_group_and_non_text_messages_do_not_reach_chat_service(self):
        handlers = self.make_handlers()
        group = private_update()
        group.effective_chat.type = "group"
        asyncio.run(handlers.text_message(group, None))
        image = private_update(text=None)
        asyncio.run(handlers.non_text_message(image, None))
        self.assertEqual(self.chat.calls, [])
        self.assertIn("chat pribadi", group.effective_message.replies[0][0])
        self.assertIn("pesan teks", image.effective_message.replies[0][0])

    def test_long_reply_is_safe_plain_text_chunks(self):
        handlers = self.make_handlers("x" * 9000)
        update = private_update()
        asyncio.run(handlers.text_message(update, None))
        chunks = update.effective_message.replies
        self.assertEqual("".join(chunk[0] for chunk in chunks), "x" * 9000)
        self.assertTrue(all(len(chunk[0]) <= 4096 for chunk in chunks))
        self.assertTrue(all(chunk[1] == {} for chunk in chunks))

    def test_same_customer_session_is_stable_and_different_user_isolated(self):
        handlers = self.make_handlers()
        asyncio.run(handlers.text_message(private_update(user_id=1, chat_id=8), None))
        asyncio.run(handlers.text_message(private_update(user_id=1, chat_id=8), None))
        asyncio.run(handlers.text_message(private_update(user_id=2, chat_id=8), None))
        calls = self.chat.calls
        self.assertEqual(calls[0]["session_reference"], calls[1]["session_reference"])
        self.assertNotEqual(calls[0]["session_reference"], calls[2]["session_reference"])
        self.assertNotEqual(calls[0]["customer"].id, calls[2]["customer"].id)

    def test_missing_update_objects_never_open_identity_or_chat(self):
        handlers = self.make_handlers()
        missing_message = private_update()
        missing_message.effective_message = None
        missing_user = private_update()
        missing_user.effective_user = None
        missing_chat = private_update()
        missing_chat.effective_chat = None
        for update in (None, missing_message, missing_user, missing_chat):
            asyncio.run(handlers.text_message(update, None))
        self.assertEqual(self.identity.calls, [])
        self.assertEqual(self.chat.calls, [])

    def test_all_non_private_chat_types_are_rejected_before_identity(self):
        handlers = self.make_handlers()
        for chat_type in ("group", "supergroup", "channel"):
            update = private_update()
            update.effective_chat.type = chat_type
            asyncio.run(handlers.text_message(update, None))
            self.assertIn("chat pribadi", update.effective_message.replies[0][0])
        self.assertEqual(self.identity.calls, [])
        self.assertEqual(self.chat.calls, [])

    def test_command_text_cannot_reach_general_chat_flow(self):
        handlers = self.make_handlers()
        update = private_update(text="/unknown")
        asyncio.run(handlers.text_message(update, None))
        self.assertEqual(self.identity.calls, [])
        self.assertEqual(self.chat.calls, [])

    def test_send_failure_has_one_fallback_then_stops_and_closes_db(self):
        handlers = self.make_handlers()
        update = private_update()
        update.effective_message = FakeMessage("halo", failures=2)
        asyncio.run(handlers.text_message(update, None))
        self.assertEqual(update.effective_message.calls, 2)
        self.assertEqual(self.db.rollbacks, 1)
        self.assertTrue(self.db.closed)

    def test_status_is_deterministic_and_does_not_call_general_chat(self):
        handlers = self.make_handlers()
        update = private_update()
        asyncio.run(handlers.status(update, None))
        self.assertEqual(self.chat.calls, [])
        self.assertEqual(len(self.chat.status_calls), 1)
        self.assertIn("tidak memiliki tiket", update.effective_message.replies[0][0])

    def test_status_returns_active_ticket_number(self):
        self.db = FakeDb()
        self.identity = FakeIdentityService()
        self.chat = FakeChatService(status_response="Tiket bantuan Anda masih aktif.\nNomor tiket Anda: CS-2026-000001")
        handlers = TelegramCustomerHandlers(
            identity_secret=IDENTITY_SECRET,
            session_factory=lambda: self.db,
            identity_service=self.identity,
            chat_service=self.chat,
        )
        update = private_update()
        asyncio.run(handlers.status(update, None))
        self.assertIn("CS-2026-000001", update.effective_message.replies[0][0])

    def test_unicode_reply_splitting_preserves_text(self):
        text = "🙂" * 5000
        chunks = split_telegram_reply(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))


class TelegramMessageUtilityTests(unittest.TestCase):
    def test_split_prefers_newline_and_obeys_limit(self):
        text = "a" * 4000 + "\n\n" + "b" * 200
        chunks = split_telegram_reply(text)
        self.assertEqual(chunks, ["a" * 4000, "b" * 200])
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))


class TelegramIdentityServiceTests(unittest.TestCase):
    class Repository:
        def __init__(self, identity):
            self.identity = identity
            self.queries = 0

        def get_by_user_key(self, db, key):
            self.queries += 1
            return self.identity

    class Db:
        def __init__(self, customer):
            self.customer = customer
            self.get_calls = 0

        def get(self, model, identifier):
            self.get_calls += 1
            return self.customer

    def test_inactive_identity_customer_and_missing_customer_fail_closed(self):
        customer_id = uuid4()
        cases = (
            (SimpleNamespace(is_active=False, customer_id=customer_id), SimpleNamespace(id=customer_id, is_active=True)),
            (SimpleNamespace(is_active=True, customer_id=customer_id), SimpleNamespace(id=customer_id, is_active=False)),
            (SimpleNamespace(is_active=True, customer_id=customer_id), None),
        )
        for identity, customer in cases:
            with self.subTest(identity_active=identity.is_active, customer=customer):
                service = TelegramIdentityService(self.Repository(identity))
                with self.assertRaises(TelegramIdentityUnavailableError):
                    service.resolve_or_create(self.Db(customer), telegram_user_id=1, identity_secret=IDENTITY_SECRET)

    def test_repository_rejects_missing_customer_before_database_mutation(self):
        class Db:
            def add(self, value):
                raise AssertionError("Repository must reject before db.add")
        with self.assertRaises(ValueError):
            TelegramIdentityRepository().add(
                Db(), telegram_user_key="a" * 64, customer_id=None
            )


class AuthenticatedTicketStatusTests(unittest.TestCase):
    def test_status_is_owner_and_session_scoped_without_agent_handle(self):
        calls = []
        class Tickets:
            def get_active(self, db, *, owner_customer_id, memory_key):
                calls.append((owner_customer_id, memory_key))
                return getattr(db, "ticket", None)
        class Agent:
            handoff_service = SimpleNamespace(ticket_service=Tickets())
            async def handle(self, **kwargs):
                raise AssertionError("AI/orchestrator must not be called for status")
        service = AuthenticatedChatService(agent=Agent())
        owner_a = uuid4()
        owner_b = uuid4()
        active_db = SimpleNamespace(ticket=SimpleNamespace(ticket_number="CS-2026-000007"))
        self.assertIn(
            "CS-2026-000007",
            service.ticket_status(db=active_db, customer=SimpleNamespace(id=owner_a), session_reference="session"),
        )
        self.assertIn(
            "tidak memiliki tiket",
            service.ticket_status(db=SimpleNamespace(ticket=None), customer=SimpleNamespace(id=owner_b), session_reference="session"),
        )
        self.assertEqual(calls[0][0], owner_a)
        self.assertEqual(calls[1][0], owner_b)
        self.assertNotEqual(calls[0][1], calls[1][1])


class TelegramLoggingTests(unittest.TestCase):
    def test_redaction_and_safe_ptb_error_logging(self):
        fake_token = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
        raw_message = "pesan-pelanggan-sangat-rahasia"
        full_reply = "balasan-aura-lengkap-yang-rahasia"
        raw_id = "987654321012345"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            logger.warning(
                "failure url=https://api.telegram.org/bot%s/getMe auth=Bearer private-value "
                "dsn=postgresql+psycopg://user:password@localhost/db",
                fake_token,
            )
            error = RuntimeError(
                f"{fake_token} {raw_message} {full_reply} {raw_id}"
            )
            asyncio.run(safe_ptb_error_handler(SimpleNamespace(secret=raw_message), SimpleNamespace(error=error)))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        output = stream.getvalue()
        self.assertNotIn(fake_token, output)
        self.assertNotIn(fake_token.split(":", 1)[1], output)
        self.assertNotIn("/bot" + fake_token + "/", output)
        self.assertNotIn("Bearer private-value", output)
        self.assertNotIn("user:password@", output)
        self.assertNotIn(raw_message, output)
        self.assertNotIn(full_reply, output)
        self.assertNotIn(raw_id, output)
        self.assertIn("category=ptb_update_error", output)

    def test_httpx_mock_transport_never_logs_bot_url_token(self):
        configure_safe_logging()
        fake_token = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
        stream = io.StringIO()
        capture = logging.StreamHandler(stream)
        capture.setFormatter(RedactingFormatter("%(levelname)s %(name)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(capture)

        async def request():
            async def response_handler(request):
                return httpx.Response(200, json={"ok": True}, request=request)
            async with httpx.AsyncClient(transport=httpx.MockTransport(response_handler)) as client:
                await client.get(f"https://api.telegram.org/bot{fake_token}/getMe")

        try:
            asyncio.run(request())
        finally:
            root.removeHandler(capture)
        output = stream.getvalue()
        self.assertNotIn(fake_token, output)
        self.assertNotIn(fake_token.split(":", 1)[1], output)
        self.assertNotIn("/bot" + fake_token + "/", output)
