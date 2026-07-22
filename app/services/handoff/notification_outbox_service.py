from app.db.repositories.support_ticket_notification_repository import (
    SupportTicketNotificationRepository,
)


class NotificationOutboxService:
    def __init__(self, repository=None):
        self.repository = repository or SupportTicketNotificationRepository()

    def enqueue_new_ticket(self, db, *, ticket):
        if ticket is None or getattr(ticket, "id", None) is None:
            raise ValueError("A persisted support ticket is required.")
        return self.repository.add_pending(db, support_ticket_id=ticket.id)

    def claim_due(self, db, *, lease_seconds: int):
        return self.repository.claim_due(db, lease_seconds=lease_seconds)

    def mark_sent(self, db, **kwargs):
        return self.repository.mark_sent(db, **kwargs)

    def mark_failed_attempt(self, db, **kwargs):
        return self.repository.mark_failed_attempt(db, **kwargs)

