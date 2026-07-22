import hashlib

from sqlalchemy.exc import IntegrityError

from app.core.ownership import require_owner_customer_id
from app.db.models.support_ticket import validate_ticket_fields
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.services.handoff.notification_outbox_service import NotificationOutboxService


class TicketService:
    """Persistent ticket creation with no raw session or conversation storage."""

    def __init__(self, repository=None, notification_service=None):
        self.repository = repository or SupportTicketRepository()
        # Production repositories always receive the transactional outbox.
        # Lightweight repositories injected by existing unit tests remain
        # usable unless an outbox test explicitly injects its service.
        self.notification_service = notification_service
        if notification_service is None and isinstance(self.repository, SupportTicketRepository):
            self.notification_service = NotificationOutboxService()

    @staticmethod
    def hash_session_reference(memory_key: str) -> str:
        return hashlib.sha256(memory_key.encode("utf-8")).hexdigest()

    def create_or_get(self, db, *, owner_customer_id, memory_key, handoff_state):
        require_owner_customer_id(owner_customer_id)
        category = handoff_state["category"]
        reason_code = handoff_state["reason_code"]
        priority = handoff_state["priority"]
        validate_ticket_fields(
            category=category,
            reason_code=reason_code,
            priority=priority,
            status="open",
        )
        session_hash = self.hash_session_reference(memory_key)
        existing = self.repository.get_active_by_owner_and_session_hash(
            db,
            owner_customer_id,
            session_hash,
        )
        if existing is not None:
            return existing
        try:
            create_values = dict(
                owner_customer_id=owner_customer_id,
                session_reference_hash=session_hash,
                category=category,
                reason_code=reason_code,
                priority=priority,
                attempt_count=handoff_state["attempt_count"],
            )
            if self.notification_service is None:
                return self.repository.create(db, **create_values)
            ticket = self.repository.create(db, **create_values)
            self.notification_service.enqueue_new_ticket(db, ticket=ticket)
            db.commit()
            db.refresh(ticket)
            return ticket
        except IntegrityError:
            # The repository has already rolled back its failed INSERT. Calling
            # rollback again is safe for SQLAlchemy sessions and also protects a
            # repository implementation that raised before it could do so.
            db.rollback()
            existing = self.repository.get_active_by_owner_and_session_hash(
                db,
                owner_customer_id,
                session_hash,
            )
            if existing is not None:
                return existing
            raise
        except Exception:
            db.rollback()
            raise

    def get_active(self, db, *, owner_customer_id, memory_key):
        require_owner_customer_id(owner_customer_id)
        session_hash = self.hash_session_reference(memory_key)
        return self.repository.get_active_by_owner_and_session_hash(
            db,
            owner_customer_id,
            session_hash,
        )

    def mark_in_progress(self, db, *, ticket_id: int, owner_customer_id):
        require_owner_customer_id(owner_customer_id)
        return self.repository.mark_in_progress(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
        )

    def resolve(self, db, *, ticket_id: int, owner_customer_id):
        require_owner_customer_id(owner_customer_id)
        return self.repository.resolve(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
        )

    def close(self, db, *, ticket_id: int, owner_customer_id):
        require_owner_customer_id(owner_customer_id)
        return self.repository.close(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
        )
