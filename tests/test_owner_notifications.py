"""Offline Phase E tests; no test in this module contacts Telegram."""

import asyncio
import io
import logging
import os
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.logger import RedactingFormatter, logger
from app.db.base import Base
from app.db.models.customer import Customer
from app.db.models.support_ticket import SAFE_TICKET_SUMMARIES, SupportTicket
from app.db.models.support_ticket_notification import SupportTicketNotification
from app.db.repositories.support_ticket_notification_repository import SupportTicketNotificationRepository
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.integrations.telegram.owner_notification_dispatcher import (
    OwnerNotificationDispatcher,
    classify_telegram_failure,
)
from app.integrations.telegram.owner_notification_renderer import render_owner_notification
from app.integrations.telegram.runner import (
    TelegramRunnerConfigurationError,
    prepare_polling,
    shutdown_owner_notifications,
    validate_runner_configuration,
)
from app.services.handoff.notification_outbox_service import NotificationOutboxService
from app.services.handoff.ticket_service import TicketService


VALID_TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
IDENTITY_SECRET = "telegram-identity-secret-that-is-long-enough"


def runner_config(**overrides):
    values = dict(
        APP_ENV="test",
        TELEGRAM_BOT_TOKEN=VALID_TOKEN,
        TELEGRAM_IDENTITY_SECRET=IDENTITY_SECRET,
        TELEGRAM_CLEAR_WEBHOOK_ON_START=False,
        TELEGRAM_DROP_PENDING_UPDATES=False,
        TELEGRAM_POLL_TIMEOUT_SECONDS=30,
        TELEGRAM_OWNER_NOTIFICATIONS_ENABLED=False,
        TELEGRAM_OWNER_CHAT_ID=None,
        TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS=5,
        TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS=5,
        TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS=10,
        TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS=60,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class OwnerNotificationConfigurationTests(unittest.TestCase):
    def test_fastapi_settings_tolerate_missing_and_malformed_runner_only_values(self):
        configured = Settings(
            _env_file=None,
            APP_ENV="test",
            DATABASE_URL="sqlite://",
            AUTH_JWT_SECRET="test-fastapi-secret-not-for-production-12345",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="test-model",
            TELEGRAM_OWNER_NOTIFICATIONS_ENABLED="malformed",
            TELEGRAM_OWNER_COMMANDS_ENABLED="malformed",
            TELEGRAM_OWNER_CHAT_ID="secret-invalid-id",
            TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS="bad",
        )
        self.assertFalse(hasattr(configured, "TELEGRAM_OWNER_NOTIFICATIONS_ENABLED"))
        self.assertFalse(hasattr(configured, "TELEGRAM_OWNER_COMMANDS_ENABLED"))
        self.assertFalse(hasattr(configured, "TELEGRAM_OWNER_CHAT_ID"))

    def test_enabled_requires_strict_private_owner_chat_id(self):
        for value in (None, "", " ", True, False, 0, -1, 1.0, "1.0", "-5", "abc", 2**63):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(TelegramRunnerConfigurationError) as context:
                    validate_runner_configuration(runner_config(
                        TELEGRAM_OWNER_NOTIFICATIONS_ENABLED=True,
                        TELEGRAM_OWNER_CHAT_ID=value,
                    ))
                if isinstance(value, str) and len(value.strip()) > 2:
                    self.assertNotIn(value, str(context.exception))

        result = validate_runner_configuration(runner_config(
            TELEGRAM_OWNER_NOTIFICATIONS_ENABLED="true",
            TELEGRAM_OWNER_CHAT_ID="123456",
        ))
        self.assertTrue(result.owner_notifications_enabled)
        self.assertEqual(result.owner_chat_id, 123456)

    def test_owner_boolean_and_integer_settings_are_strict_and_bounded(self):
        for name, values in {
            "TELEGRAM_OWNER_NOTIFICATIONS_ENABLED": (1, "yes", "1", None),
            "TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS": (True, 0, 301, 1.0, "1.0"),
            "TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS": (False, 0, 21, "bad"),
            "TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS": (True, -1, 3601),
            "TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS": (True, 0, 4, 3601),
        }.items():
            for value in values:
                with self.subTest(name=name, value_type=type(value).__name__):
                    with self.assertRaises(TelegramRunnerConfigurationError):
                        validate_runner_configuration(runner_config(**{name: value}))


class OwnerNotificationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Customer.__table__.create(self.engine)
        SupportTicket.__table__.create(self.engine)
        SupportTicketNotification.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.owner = uuid4()
        with self.Session.begin() as db:
            db.add(Customer(id=self.owner))

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def state():
        return {
            "category": "explicit_human_request",
            "reason_code": "explicit_human_request",
            "priority": "high",
            "attempt_count": 1,
        }

    def test_new_ticket_and_one_pending_job_commit_atomically_and_reuse_does_not_duplicate(self):
        db = self.Session()
        try:
            service = TicketService()
            first = service.create_or_get(
                db, owner_customer_id=self.owner, memory_key="owner:session", handoff_state=self.state()
            )
            second = service.create_or_get(
                db, owner_customer_id=self.owner, memory_key="owner:session", handoff_state=self.state()
            )
            rows = db.execute(select(SupportTicketNotification)).scalars().all()
            self.assertEqual(first.id, second.id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].support_ticket_id, first.id)
            self.assertEqual(rows[0].status, "pending")
            third = service.create_or_get(
                db, owner_customer_id=self.owner, memory_key="owner:other-session", handoff_state=self.state()
            )
            self.assertNotEqual(first.id, third.id)
            self.assertEqual(len(db.execute(select(SupportTicketNotification)).scalars().all()), 2)
        finally:
            db.close()

    def test_outbox_failure_rolls_back_ticket_and_session_remains_usable(self):
        class FailingOutbox:
            def enqueue_new_ticket(self, db, *, ticket):
                raise RuntimeError("synthetic persistence failure")

        db = self.Session()
        try:
            service = TicketService(SupportTicketRepository(), FailingOutbox())
            with self.assertRaises(RuntimeError):
                service.create_or_get(
                    db, owner_customer_id=self.owner, memory_key="owner:failed", handoff_state=self.state()
                )
            self.assertEqual(db.execute(select(SupportTicket)).scalars().all(), [])
            self.assertEqual(db.execute(select(SupportTicketNotification)).scalars().all(), [])
            self.assertEqual(db.execute(select(Customer)).scalars().one().id, self.owner)
        finally:
            db.close()


class OwnerNotificationRendererTests(unittest.TestCase):
    def test_renderer_is_plain_bounded_allowlisted_and_unicode_safe(self):
        customer_uuid = str(uuid4())
        raw = f"Rizal 7 orang 21-07 19:00 {customer_uuid}\x00TOKEN"
        ticket = SimpleNamespace(
            ticket_number="CS-2026-000003",
            category="explicit_human_request",
            priority="high",
            status="open",
            safe_summary=raw,
            created_at=datetime(2026, 7, 21, 13, 48, tzinfo=timezone.utc),
        )
        chunks = render_owner_notification(ticket)
        message = "".join(chunks)
        self.assertIn("Tiket bantuan baru", message)
        self.assertIn("Permintaan bantuan petugas", message)
        self.assertIn("21 Juli 2026", message)
        self.assertNotIn(raw, message)
        self.assertNotIn(customer_uuid, message)
        self.assertNotIn("\x00", message)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        self.assertNotIn("parse_mode", message)

    def test_unknown_values_are_not_echoed(self):
        ticket = SimpleNamespace(
            ticket_number="attacker-number",
            category="private-category-value",
            priority="private-priority-value",
            status="private-status-value",
            safe_summary="private raw message",
            created_at=None,
        )
        message = "".join(render_owner_notification(ticket))
        for value in vars(ticket).values():
            if isinstance(value, str):
                self.assertNotIn(value, message)


class OwnerNotificationSchemaTests(unittest.TestCase):
    def test_model_has_named_integrity_constraints_and_ordered_indexes(self):
        table = SupportTicketNotification.__table__
        names = {constraint.name for constraint in table.constraints}
        self.assertTrue({
            "pk_support_ticket_notifications",
            "fk_support_ticket_notifications_support_ticket_id",
            "uq_support_ticket_notifications_ticket_channel",
            "ck_support_ticket_notifications_channel",
            "ck_support_ticket_notifications_status",
            "ck_support_ticket_notifications_attempt_count",
        }.issubset(names))
        indexes = {index.name: [column.name for column in index.columns] for index in table.indexes}
        self.assertEqual(indexes["ix_support_ticket_notifications_status_next_attempt"], ["status", "next_attempt_at"])
        self.assertEqual(indexes["ix_support_ticket_notifications_status_lease"], ["status", "lease_expires_at"])

    def test_migration_source_is_additive_and_has_no_startup_hook(self):
        source = (Path(__file__).resolve().parents[1] / "migrations" / "add_support_ticket_notifications.py").read_text(encoding="utf-8").upper()
        for forbidden in ("DROP TABLE", "DROP COLUMN", "DELETE FROM", "TRUNCATE", "ALTER TABLE RESERVATIONS", "ALTER TABLE CUSTOMERS", "ALTER TABLE TELEGRAM_IDENTITIES"):
            self.assertNotIn(forbidden, source)
        repository_source = (
            Path(__file__).resolve().parents[1]
            / "app" / "db" / "repositories"
            / "support_ticket_notification_repository.py"
        ).read_text(encoding="utf-8").upper()
        self.assertIn("WITH_FOR_UPDATE(SKIP_LOCKED=TRUE)", repository_source)


class OwnerNotificationRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Customer.__table__.create(self.engine)
        SupportTicket.__table__.create(self.engine)
        SupportTicketNotification.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        owner = uuid4()
        now = datetime.now(timezone.utc)
        with self.Session.begin() as db:
            db.add(Customer(id=owner))
            ticket = SupportTicket(
                ticket_number="CS-2026-000001", owner_customer_id=owner,
                session_reference_hash="a" * 64, category="explicit_human_request",
                reason_code="explicit_human_request", priority="high",
                safe_summary=SAFE_TICKET_SUMMARIES["explicit_human_request"], status="open",
                attempt_count=1, created_at=now, updated_at=now,
            )
            db.add(ticket)
            db.flush()
            self.ticket_id = ticket.id

    def tearDown(self):
        self.engine.dispose()

    def test_claim_once_success_not_reselected_and_expired_lease_recovers(self):
        repository = SupportTicketNotificationRepository()
        db = self.Session()
        now = datetime.now(timezone.utc)
        try:
            row = repository.add_pending(db, support_ticket_id=self.ticket_id, now=now)
            db.commit()
            claimed = repository.claim_due(db, lease_seconds=60, now=now)
            self.assertEqual(claimed.id, row.id)
            self.assertIsNone(repository.claim_due(db, lease_seconds=60, now=now))
            claimed.lease_expires_at = now - timedelta(seconds=1)
            db.commit()
            recovered = repository.claim_due(db, lease_seconds=60, now=now)
            self.assertEqual(recovered.id, row.id)
            repository.mark_sent(db, notification_id=row.id, telegram_message_id=77, now=now)
            self.assertIsNone(repository.claim_due(db, lease_seconds=60, now=now + timedelta(days=1)))
        finally:
            db.close()

    def test_retry_backoff_permanent_and_max_attempts(self):
        repository = SupportTicketNotificationRepository()
        db = self.Session()
        now = datetime.now(timezone.utc)
        try:
            row = repository.add_pending(db, support_ticket_id=self.ticket_id, now=now)
            db.commit()
            repository.claim_due(db, lease_seconds=60, now=now)
            retry = repository.mark_failed_attempt(
                db, notification_id=row.id, error_code="timeout", retryable=True,
                max_attempts=2, retry_base_seconds=10, retry_after_seconds=20, now=now,
            )
            self.assertEqual(retry.status, "pending")
            self.assertEqual(
                retry.next_attempt_at.replace(tzinfo=timezone.utc),
                now + timedelta(seconds=20),
            )
            repository.claim_due(db, lease_seconds=60, now=retry.next_attempt_at)
            failed = repository.mark_failed_attempt(
                db, notification_id=row.id, error_code="timeout", retryable=True,
                max_attempts=2, retry_base_seconds=10, now=retry.next_attempt_at,
            )
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.attempt_count, 2)
        finally:
            db.close()

    def test_repository_leaves_database_failure_rollback_to_service(self):
        class FailingDb:
            def __init__(self):
                self.rollbacks = 0
            def execute(self, _statement):
                raise RuntimeError("synthetic database failure")
            def rollback(self):
                self.rollbacks += 1
        db = FailingDb()
        with self.assertRaises(RuntimeError):
            SupportTicketNotificationRepository().claim_due(db, lease_seconds=60)
        self.assertEqual(db.rollbacks, 0)


class OwnerNotificationDispatcherTests(unittest.TestCase):
    def test_failure_classification_does_not_require_logging_exception_text(self):
        class RetryAfter(Exception):
            retry_after = 17
        self.assertEqual(classify_telegram_failure(TimeoutError()).code, "timeout")
        result = classify_telegram_failure(RetryAfter("private response"))
        self.assertEqual((result.code, result.retry_after_seconds), ("rate_limited", 17))

    def test_runner_starts_once_after_webhook_and_shuts_down(self):
        config = validate_runner_configuration(runner_config(
            TELEGRAM_OWNER_NOTIFICATIONS_ENABLED=True,
            TELEGRAM_OWNER_CHAT_ID=123456,
        ))
        bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")))
        application = SimpleNamespace(
            bot=bot,
            bot_data={"aura_runner_config": config, "aura_session_factory": object()},
        )

        class FakeDispatcher:
            instances = []
            def __init__(self, **kwargs):
                self.stopped = False
                self.event = asyncio.Event()
                self.__class__.instances.append(self)
            async def run(self):
                await self.event.wait()
            def stop(self):
                self.stopped = True
                self.event.set()

        async def exercise():
            with patch("app.integrations.telegram.runner.OwnerNotificationDispatcher", FakeDispatcher):
                await prepare_polling(application)
                first = application.bot_data["aura_owner_notification_task"]
                await prepare_polling(application)
                self.assertIs(first, application.bot_data["aura_owner_notification_task"])
                await shutdown_owner_notifications(application)
        asyncio.run(exercise())
        self.assertEqual(len(FakeDispatcher.instances), 1)
        self.assertTrue(FakeDispatcher.instances[0].stopped)

    def test_disabled_runner_does_not_start_dispatcher(self):
        config = validate_runner_configuration(runner_config())
        application = SimpleNamespace(
            bot=SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url=""))),
            bot_data={"aura_runner_config": config, "aura_session_factory": object()},
        )
        with patch("app.integrations.telegram.runner.OwnerNotificationDispatcher") as dispatcher:
            asyncio.run(prepare_polling(application))
        dispatcher.assert_not_called()

    def test_one_failed_job_does_not_stop_the_next_job(self):
        ticket = SimpleNamespace(
            ticket_number="CS-2026-000001", category="explicit_human_request",
            priority="high", status="open",
            safe_summary=SAFE_TICKET_SUMMARIES["explicit_human_request"],
            created_at=datetime.now(timezone.utc),
        )
        jobs = [
            SimpleNamespace(id=1, support_ticket_id=11),
            SimpleNamespace(id=2, support_ticket_id=12),
        ]

        class Db:
            def get(self, _model, _identifier):
                return ticket
            def close(self):
                pass

        class Outbox:
            def __init__(self):
                self.failed = []
                self.sent = []
            def claim_due(self, _db, **_kwargs):
                return jobs.pop(0) if jobs else None
            def mark_failed_attempt(self, _db, **kwargs):
                self.failed.append(kwargs)
            def mark_sent(self, _db, **kwargs):
                self.sent.append(kwargs)

        class PrivateNetworkError(Exception):
            pass

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[
            PrivateNetworkError("raw private Telegram response"),
            SimpleNamespace(message_id=91),
        ]))
        outbox = Outbox()
        dispatcher = OwnerNotificationDispatcher(
            bot=bot,
            session_factory=Db,
            owner_chat_id=123456,
            config=SimpleNamespace(
                owner_notification_lease_seconds=60,
                owner_notification_max_attempts=5,
                owner_notification_retry_base_seconds=10,
                owner_notification_poll_seconds=5,
            ),
            outbox_service=outbox,
        )
        asyncio.run(dispatcher.process_once())
        asyncio.run(dispatcher.process_once())
        self.assertEqual(len(outbox.failed), 1)
        self.assertEqual(len(outbox.sent), 1)
        self.assertEqual(outbox.sent[0]["notification_id"], 2)


class OwnerNotificationPrivacyLoggingTests(unittest.TestCase):
    def test_safe_dispatch_log_contains_code_not_private_values(self):
        private_values = [
            VALID_TOKEN, "/bot" + VALID_TOKEN + "/", "123456789012345",
            str(uuid4()), "raw customer message", "private exception response",
        ]
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(message)s"))
        logger.addHandler(handler)
        try:
            with patch.dict(os.environ, {"TELEGRAM_OWNER_CHAT_ID": private_values[2]}):
                logger.warning(
                    "OWNER NOTIFICATION: operation=send notification_id=1 status=failed "
                    "code=timeout accidental_recipient=%s",
                    private_values[2],
                )
        finally:
            logger.removeHandler(handler)
        output = stream.getvalue()
        self.assertIn("code=timeout", output)
        for value in private_values:
            self.assertNotIn(value, output)


if __name__ == "__main__":
    unittest.main()
