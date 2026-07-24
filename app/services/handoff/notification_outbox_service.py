from dataclasses import dataclass
from datetime import datetime

from app.core.unit_of_work import UnitOfWork
from app.db.repositories.support_ticket_notification_repository import (
    SupportTicketNotificationRepository,
)


@dataclass(frozen=True)
class SupportTicketNotificationDTO:
    id: int
    support_ticket_id: int
    channel: str
    status: str
    attempt_count: int
    next_attempt_at: datetime
    lease_expires_at: datetime | None
    sent_at: datetime | None
    telegram_message_id: int | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class NotificationOutboxService:
    def __init__(self, repository=None):
        self.repository = repository or SupportTicketNotificationRepository()

    @staticmethod
    def _dto(notification) -> SupportTicketNotificationDTO | None:
        if notification is None:
            return None
        return SupportTicketNotificationDTO(
            id=int(notification.id),
            support_ticket_id=int(notification.support_ticket_id),
            channel=str(notification.channel),
            status=str(notification.status),
            attempt_count=int(notification.attempt_count),
            next_attempt_at=notification.next_attempt_at,
            lease_expires_at=notification.lease_expires_at,
            sent_at=notification.sent_at,
            telegram_message_id=notification.telegram_message_id,
            last_error_code=notification.last_error_code,
            created_at=notification.created_at,
            updated_at=notification.updated_at,
        )

    def enqueue_new_ticket(
        self,
        db,
        *,
        ticket,
    ):
        if ticket is None or getattr(ticket, "id", None) is None:
            raise ValueError("A persisted support ticket is required.")
        with UnitOfWork(db) as unit:
            notification = self.repository.add_pending(
                db,
                support_ticket_id=ticket.id,
            )
            result = self._dto(notification)
            unit.commit()
        return result

    def _stage_new_ticket(self, db, *, ticket):
        """Stage a row inside TicketService's already-owned transaction."""
        if ticket is None or getattr(ticket, "id", None) is None:
            raise ValueError("A persisted support ticket is required.")
        return self.repository.add_pending(db, support_ticket_id=ticket.id)

    def claim_due(self, db, *, lease_seconds: int):
        with UnitOfWork(db) as unit:
            notification = self.repository.claim_due(
                db,
                lease_seconds=lease_seconds,
            )
            result = self._dto(notification)
            unit.commit()
        return result

    def mark_sent(self, db, **kwargs):
        with UnitOfWork(db) as unit:
            notification = self.repository.mark_sent(db, **kwargs)
            result = self._dto(notification)
            unit.commit()
        return result

    def mark_failed_attempt(self, db, **kwargs):
        with UnitOfWork(db) as unit:
            notification = self.repository.mark_failed_attempt(db, **kwargs)
            result = self._dto(notification)
            unit.commit()
        return result
