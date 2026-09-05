"""Opt-in PostgreSQL transaction tests for G1D-A1.

This module never falls back to DATABASE_URL and creates model tables directly
inside a disposable schema; it does not execute application migrations.
"""

import os
import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from tests.integration.reservation_clock import install_reservation_clock
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
)
from app.db.models.customer import Customer
from app.db.models.reservation import Reservation
from app.db.models.support_ticket import SupportTicket
from app.db.models.support_ticket_notification import SupportTicketNotification
from app.db.repositories.reservation_repository import ReservationRepository
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.owner_notification_dispatcher import (
    OwnerNotificationDispatcher,
)
from app.schemas.reservation import ReservationCreate
from app.services.handoff.notification_outbox_service import NotificationOutboxService
from app.services.handoff.owner_ticket_service import OwnerTicketService
from app.services.handoff.ticket_service import TicketService
from app.services.reservation.service import ReservationService
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
)


def _identity(url):
    parsed = make_url(url)
    return parsed.get_backend_name(), parsed.host, parsed.port, parsed.database


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return "TEST_DATABASE_URL is not configured; G1D-A1 PostgreSQL tests are skipped."
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if not parsed.database or "test" not in parsed.database.lower():
            return "TEST_DATABASE_URL must name a disposable database containing 'test'."
        if _identity(value) == _identity(settings.DATABASE_URL):
            return "TEST_DATABASE_URL resolves to the normal database; refusing to run."
    except Exception:
        return "TEST_DATABASE_URL is invalid; G1D-A1 PostgreSQL tests are skipped."
    return None


SKIP_REASON = _skip_reason()


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestG1DTransactionsPostgreSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)

    def setUp(self):
        install_reservation_clock(self)
        self.schema = f"aura_g1d_a1_{uuid4().hex[:12]}"
        self.engine = None
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        # Register cleanup immediately so partial setUp failures cannot leak a
        # disposable schema.
        self.addCleanup(self._cleanup_schema)
        url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict(
            {"options": f"-csearch_path={self.schema},public"}
        )
        self.engine = create_engine(url, pool_pre_ping=True)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=True)
        Customer.__table__.create(self.engine)
        Reservation.__table__.create(self.engine)
        SupportTicket.__table__.create(self.engine)
        SupportTicketNotification.__table__.create(self.engine)
        self.owner = uuid4()
        with self.Session.begin() as db:
            db.add(Customer(id=self.owner))

    def _cleanup_schema(self):
        if self.engine is not None:
            self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    @staticmethod
    def reservation_data(people=4):
        return ReservationCreate(
            name="Rizal",
            people=people,
            date="2026-08-01",
            time="19:00",
        )

    @staticmethod
    def handoff_state():
        return {
            "category": "explicit_human_request",
            "reason_code": "explicit_human_request",
            "priority": "high",
            "attempt_count": 1,
        }

    def count(self, model):
        with self.Session() as db:
            return db.scalar(select(func.count()).select_from(model))

    def test_reservation_create_update_and_cancel_are_atomic(self):
        with self.Session() as db:
            created = ReservationService().create_reservation(
                db,
                self.reservation_data(),
                owner_customer_id=self.owner,
            )
        self.assertGreater(created.id, 0)
        self.assertEqual(self.count(Reservation), 1)

        class FailingUpdateRepository(ReservationRepository):
            def update_reservation_field_by_public_reference(self, *args, **kwargs):
                super().update_reservation_field_by_public_reference(*args, **kwargs)
                raise RuntimeError("controlled pre-commit failure")

        with self.Session() as db:
            with self.assertRaises(PersistenceOperationError):
                ReservationService(FailingUpdateRepository()).update_reservation_field_by_reference(
                    db,
                    created.reference,
                    "people",
                    8,
                    owner_customer_id=self.owner,
                )
        with self.Session() as db:
            self.assertEqual(db.get(Reservation, created.id).people, 4)

        class FailingCancelRepository(ReservationRepository):
            def cancel_reservation_by_public_reference(self, *args, **kwargs):
                super().cancel_reservation_by_public_reference(*args, **kwargs)
                raise RuntimeError("controlled pre-commit failure")

        with self.Session() as db:
            with self.assertRaises(PersistenceOperationError):
                ReservationService(FailingCancelRepository()).cancel_reservation_by_reference(
                    db,
                    created.reference,
                    owner_customer_id=self.owner,
                )
        with self.Session() as db:
            self.assertEqual(db.get(Reservation, created.id).status, "pending")

    def test_successful_update_and_cancel_commit_detached_results(self):
        with self.Session() as db:
            created = ReservationService().create_reservation(
                db,
                self.reservation_data(),
                owner_customer_id=self.owner,
            )
        with self.Session() as db:
            updated = ReservationService().update_reservation_field_by_reference(
                db,
                created.reference,
                "people",
                8,
                owner_customer_id=self.owner,
            )
        self.assertEqual(updated.people, 8)
        with self.Session() as db:
            cancelled = ReservationService().cancel_reservation_by_reference(
                db,
                created.reference,
                owner_customer_id=self.owner,
            )
        self.assertEqual(cancelled.status, "cancelled")
        with self.Session() as db:
            persisted = db.get(Reservation, created.id)
            self.assertEqual(persisted.people, 8)
            self.assertEqual(persisted.status, "cancelled")

    def test_legacy_persisted_row_is_safe_with_expire_on_commit(self):
        with self.Session.begin() as db:
            row = Reservation(
                name="Legacy / Imported",
                people=25,
                date="01/08/2026",
                time="7pm",
                status="pending",
                owner_customer_id=self.owner,
            )
            db.add(row)
            db.flush()
            reservation_id = row.id

        with self.Session() as db:
            with self.assertRaises(PublicReservationReferenceUnavailableError):
                ReservationService().list_recent_reservations(
                    db,
                    owner_customer_id=self.owner,
                )
        self.assertGreater(reservation_id, 0)

    def test_forced_create_failure_leaves_no_reservation_and_session_is_usable(self):
        class FailingCreateRepository(ReservationRepository):
            def create(self, *args, **kwargs):
                super().create(*args, **kwargs)
                raise RuntimeError("controlled pre-commit failure")

        with self.Session() as db:
            with self.assertRaises(PersistenceOperationError):
                ReservationService(FailingCreateRepository()).create_reservation(
                    db,
                    self.reservation_data(),
                    owner_customer_id=self.owner,
                )
            self.assertEqual(db.scalar(select(func.count()).select_from(Customer)), 1)
        self.assertEqual(self.count(Reservation), 0)

    def test_ticket_and_outbox_commit_or_rollback_together(self):
        with self.Session() as db:
            ticket = TicketService().create_or_get(
                db,
                owner_customer_id=self.owner,
                memory_key="owner:atomic",
                handoff_state=self.handoff_state(),
            )
        self.assertGreater(ticket.id, 0)
        self.assertEqual(self.count(SupportTicket), 1)
        self.assertEqual(self.count(SupportTicketNotification), 1)

    def test_notification_unique_conflict_rolls_back_and_session_is_reusable(self):
        with self.Session() as db:
            ticket = TicketService().create_or_get(
                db,
                owner_customer_id=self.owner,
                memory_key="owner:unique",
                handoff_state=self.handoff_state(),
            )

        db = self.Session()
        try:
            with self.assertRaises(PersistenceOperationError):
                NotificationOutboxService().enqueue_new_ticket(
                    db,
                    ticket=SimpleNamespace(id=ticket.id),
                )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Customer)),
                1,
            )
        finally:
            db.close()
        self.assertEqual(self.count(SupportTicketNotification), 1)

        class FailingOutbox:
            def enqueue_new_ticket(self, db, *, ticket):
                db.add(
                    SupportTicketNotification(
                        support_ticket_id=ticket.id,
                        channel="telegram_owner",
                        status="pending",
                        attempt_count=0,
                    )
                )
                raise RuntimeError("controlled outbox failure")

        with self.Session() as db:
            with self.assertRaises(PersistenceOperationError):
                TicketService(
                    SupportTicketRepository(),
                    FailingOutbox(),
                ).create_or_get(
                    db,
                    owner_customer_id=self.owner,
                    memory_key="owner:failed",
                    handoff_state=self.handoff_state(),
                )
        self.assertEqual(self.count(SupportTicket), 1)
        self.assertEqual(self.count(SupportTicketNotification), 1)

    def test_active_ticket_race_converges_and_sessions_are_separate(self):
        sessions = []

        def create():
            db = self.Session()
            sessions.append(db)
            try:
                return TicketService().create_or_get(
                    db,
                    owner_customer_id=self.owner,
                    memory_key="owner:race",
                    handoff_state=self.handoff_state(),
                ).ticket_number
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            numbers = list(executor.map(lambda _value: create(), range(2)))
        self.assertEqual(numbers[0], numbers[1])
        self.assertIsNot(sessions[0], sessions[1])
        self.assertEqual(self.count(SupportTicket), 1)
        self.assertEqual(self.count(SupportTicketNotification), 1)

    def test_owner_take_resolve_transactions_are_terminal_safe(self):
        with self.Session() as db:
            ticket = TicketService().create_or_get(
                db,
                owner_customer_id=self.owner,
                memory_key="owner:lifecycle",
                handoff_state=self.handoff_state(),
            )
        service = OwnerTicketService()
        with self.Session() as db:
            self.assertEqual(service.take_ticket(db, ticket.ticket_number).code, "success")
        with self.Session() as db:
            self.assertEqual(service.resolve_ticket(db, ticket.ticket_number).code, "success")
        with self.Session() as db:
            self.assertEqual(
                service.take_ticket(db, ticket.ticket_number).code,
                "not_available",
            )
            persisted = db.scalar(
                select(SupportTicket).where(
                    SupportTicket.ticket_number == ticket.ticket_number
                )
            )
            self.assertEqual(persisted.status, "resolved")

    def test_send_failure_cannot_undo_committed_reservation(self):
        class Identity:
            def resolve_or_create(self, *_args, **_kwargs):
                return SimpleNamespace(id=self_owner)

        class Chat:
            async def process(self, *, db, customer, **_kwargs):
                created = ReservationService().create_reservation(
                    db,
                    TestG1DTransactionsPostgreSQL.reservation_data(),
                    owner_customer_id=customer.id,
                )
                self.created_id = created.id
                return "Reservasi tersimpan."

        self_owner = self.owner
        chat = Chat()
        reply = AsyncMock(side_effect=RuntimeError("controlled send failure"))
        update = SimpleNamespace(
            effective_message=SimpleNamespace(
                text="Halo",
                reply_text=reply,
            ),
            effective_chat=SimpleNamespace(id=101, type="private"),
            effective_user=SimpleNamespace(id=101),
        )
        handlers = TelegramCustomerHandlers(
            identity_secret="x" * 32,
            session_factory=self.Session,
            identity_service=Identity(),
            chat_service=chat,
        )
        asyncio.run(handlers.text_message(update, None))
        self.assertGreaterEqual(reply.await_count, 1)
        with self.Session() as db:
            persisted = db.get(Reservation, chat.created_id)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.status, "pending")
            self.assertEqual(persisted.people, 4)

    def test_mark_sent_outcome_unknown_keeps_sending_lease_for_recovery(self):
        with self.Session() as db:
            TicketService().create_or_get(
                db,
                owner_customer_id=self.owner,
                memory_key="owner:mark-unknown",
                handoff_state=self.handoff_state(),
            )

        class UnknownMarkOutbox(NotificationOutboxService):
            def mark_sent(self, db, **kwargs):
                raise PersistenceOutcomeUnknownError()

        dispatcher = OwnerNotificationDispatcher(
            bot=SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=77)
                )
            ),
            session_factory=self.Session,
            owner_chat_id=1,
            config=SimpleNamespace(
                owner_notification_lease_seconds=60,
                owner_notification_max_attempts=5,
                owner_notification_retry_base_seconds=10,
                owner_notification_poll_seconds=5,
            ),
            outbox_service=UnknownMarkOutbox(),
        )
        self.assertTrue(asyncio.run(dispatcher.process_once()))
        self.assertEqual(dispatcher.bot.send_message.await_count, 1)
        self.assertFalse(asyncio.run(dispatcher.process_once()))
        self.assertEqual(dispatcher.bot.send_message.await_count, 1)
        with self.Session() as db:
            notification = db.scalar(select(SupportTicketNotification))
            self.assertEqual(notification.status, "sending")
            self.assertIsNotNone(notification.lease_expires_at)


if __name__ == "__main__":
    unittest.main()
