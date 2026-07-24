import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.core.ownership import require_owner_customer_id
from app.core.transaction_errors import PersistenceOperationError
from app.core.unit_of_work import UnitOfWork
from app.db.models.support_ticket import validate_ticket_fields
from app.db.repositories.support_ticket_repository import SupportTicketRepository
from app.services.handoff.notification_outbox_service import NotificationOutboxService


@dataclass(frozen=True)
class SupportTicketDTO:
    id: int
    ticket_number: str
    category: str
    reason_code: str
    priority: str
    status: str
    attempt_count: int
    created_at: datetime | None
    updated_at: datetime | None = None
    resolved_at: datetime | None = None


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

    @staticmethod
    def _dto(ticket, *, defaults=None) -> SupportTicketDTO:
        defaults = defaults or {}
        return SupportTicketDTO(
            id=int(ticket.id),
            ticket_number=str(ticket.ticket_number),
            category=str(getattr(ticket, "category", defaults.get("category", ""))),
            reason_code=str(
                getattr(ticket, "reason_code", defaults.get("reason_code", ""))
            ),
            priority=str(getattr(ticket, "priority", defaults.get("priority", ""))),
            status=str(getattr(ticket, "status", defaults.get("status", "open"))),
            attempt_count=int(
                getattr(ticket, "attempt_count", defaults.get("attempt_count", 1))
            ),
            created_at=getattr(ticket, "created_at", None),
            updated_at=getattr(ticket, "updated_at", None),
            resolved_at=getattr(ticket, "resolved_at", None),
        )

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
        try:
            with UnitOfWork(db) as unit:
                ticket = self.repository.get_active_by_owner_and_session_hash(
                    db,
                    owner_customer_id,
                    session_hash,
                )
                if ticket is None:
                    ticket = self.repository.create(
                        db,
                        owner_customer_id=owner_customer_id,
                        session_reference_hash=session_hash,
                        category=category,
                        reason_code=reason_code,
                        priority=priority,
                        attempt_count=handoff_state["attempt_count"],
                    )
                    if self.notification_service is not None:
                        if isinstance(
                            self.notification_service,
                            NotificationOutboxService,
                        ):
                            self.notification_service._stage_new_ticket(
                                db,
                                ticket=ticket,
                            )
                        else:
                            # Injected test/domain participants stage into the
                            # transaction owned here and must not commit it.
                            self.notification_service.enqueue_new_ticket(
                                db,
                                ticket=ticket,
                            )
                result = self._dto(ticket, defaults=handoff_state)
                unit.commit()
            return result
        except PersistenceOperationError as error:
            if not isinstance(error.__cause__, IntegrityError):
                raise

        # A concurrent transaction won the active owner/session constraint.
        # The failed transaction has already been rolled back by UnitOfWork.
        with UnitOfWork(db) as unit:
            winner = self.repository.get_active_by_owner_and_session_hash(
                db,
                owner_customer_id,
                session_hash,
            )
            if winner is None:
                raise PersistenceOperationError()
            result = self._dto(winner, defaults=handoff_state)
            unit.commit()
        return result

    def get_active(self, db, *, owner_customer_id, memory_key):
        require_owner_customer_id(owner_customer_id)
        session_hash = self.hash_session_reference(memory_key)
        with UnitOfWork(db) as unit:
            ticket = self.repository.get_active_by_owner_and_session_hash(
                db,
                owner_customer_id,
                session_hash,
            )
            result = self._dto(ticket) if ticket is not None else None
            unit.commit()
        return result

    def _update_status(self, db, *, ticket_id: int, owner_customer_id, status: str):
        require_owner_customer_id(owner_customer_id)
        with UnitOfWork(db) as unit:
            ticket = self.repository.update_status(
                db,
                ticket_id=ticket_id,
                owner_customer_id=owner_customer_id,
                status=status,
            )
            result = self._dto(ticket) if ticket is not None else None
            unit.commit()
        return result

    def mark_in_progress(self, db, *, ticket_id: int, owner_customer_id):
        return self._update_status(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
            status="in_progress",
        )

    def resolve(self, db, *, ticket_id: int, owner_customer_id):
        return self._update_status(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
            status="resolved",
        )

    def close(self, db, *, ticket_id: int, owner_customer_id):
        return self._update_status(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
            status="closed",
        )
