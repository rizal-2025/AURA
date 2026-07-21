from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update

from app.core.ownership import require_owner_customer_id
from app.db.models.support_ticket import (
    ACTIVE_TICKET_STATUSES,
    SupportTicket,
    safe_summary_for,
    validate_ticket_fields,
)


class SupportTicketRepository:
    def get_active_by_owner_and_session_hash(self, db, owner_customer_id, session_reference_hash: str):
        require_owner_customer_id(owner_customer_id)
        return db.execute(select(SupportTicket).where(
            SupportTicket.owner_customer_id == owner_customer_id,
            SupportTicket.session_reference_hash == session_reference_hash,
            SupportTicket.status.in_(ACTIVE_TICKET_STATUSES),
        )).scalar_one_or_none()

    def get_by_owner_and_session_hash(self, db, owner_customer_id, session_reference_hash: str):
        """Compatibility alias; handoff reuse is intentionally active-only."""
        return self.get_active_by_owner_and_session_hash(
            db,
            owner_customer_id,
            session_reference_hash,
        )

    def create(self, db, *, owner_customer_id, session_reference_hash, category, reason_code, priority, attempt_count, status="open"):
        require_owner_customer_id(owner_customer_id)
        validate_ticket_fields(
            category=category,
            reason_code=reason_code,
            priority=priority,
            status=status,
        )
        if not isinstance(attempt_count, int) or isinstance(attempt_count, bool) or attempt_count < 1:
            raise ValueError("Support ticket attempt count must be a positive integer.")

        created_at = datetime.now(timezone.utc)
        ticket = SupportTicket(
            # A temporary unique value keeps the NOT NULL and UNIQUE invariants
            # valid until the sequence-backed public number is assigned.
            # 8-character prefix + 24 hex characters = 32 characters. The
            # 96-bit random suffix remains collision-safe while fitting the
            # database's VARCHAR(32) column until the final number is assigned.
            ticket_number=f"PENDING-{uuid4().hex[:24]}",
            owner_customer_id=owner_customer_id,
            session_reference_hash=session_reference_hash,
            category=category,
            reason_code=reason_code,
            priority=priority,
            safe_summary=safe_summary_for(category=category, reason_code=reason_code),
            status=status,
            attempt_count=attempt_count,
            created_at=created_at,
            updated_at=created_at,
        )
        try:
            db.add(ticket)
            db.flush()
            if ticket.id is None:
                raise RuntimeError("Support ticket identifier was not assigned.")
            ticket_year = ticket.created_at.astimezone(timezone.utc).year
            ticket.ticket_number = f"CS-{ticket_year}-{ticket.id:06d}"
            if ticket.ticket_number.startswith("PENDING-"):
                raise RuntimeError("Support ticket number was not assigned.")
            db.commit()
            db.refresh(ticket)
            return ticket
        except Exception:
            # Roll back both database failures and pre-commit failures so the
            # caller never receives a Session left in a failed transaction.
            db.rollback()
            raise

    def update_status(self, db, *, ticket_id: int, owner_customer_id, status: str):
        require_owner_customer_id(owner_customer_id)
        validate_ticket_fields(
            category="explicit_human_request",
            reason_code="explicit_human_request",
            priority="medium",
            status=status,
        )
        now = datetime.now(timezone.utc)
        resolved_at = now if status in {"resolved", "closed"} else None
        statement = (
            update(SupportTicket)
            .where(
                SupportTicket.id == ticket_id,
                SupportTicket.owner_customer_id == owner_customer_id,
                SupportTicket.status.in_(ACTIVE_TICKET_STATUSES),
            )
            .values(status=status, updated_at=now, resolved_at=resolved_at)
            .returning(SupportTicket)
        )
        try:
            ticket = db.execute(statement).scalar_one_or_none()
            if ticket is None:
                db.rollback()
                return None
            db.commit()
            db.refresh(ticket)
            return ticket
        except Exception:
            db.rollback()
            raise

    def mark_in_progress(self, db, *, ticket_id: int, owner_customer_id):
        return self.update_status(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
            status="in_progress",
        )

    def resolve(self, db, *, ticket_id: int, owner_customer_id):
        return self.update_status(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
            status="resolved",
        )

    def close(self, db, *, ticket_id: int, owner_customer_id):
        return self.update_status(
            db,
            ticket_id=ticket_id,
            owner_customer_id=owner_customer_id,
            status="closed",
        )
