"""Safe read model and serialized lifecycle operations for Telegram owner commands."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.repositories.support_ticket_repository import SupportTicketRepository


TICKET_NUMBER_PATTERN = re.compile(r"CS-[0-9]{4}-[0-9]{6,24}\Z")
OWNER_TICKET_RESULT_CODES = frozenset({
    "success",
    "empty",
    "invalid_argument",
    "not_available",
    "already_in_progress",
    "already_resolved",
    "closed",
    "database_error",
})


@dataclass(frozen=True)
class OwnerTicketDTO:
    ticket_number: str
    category: str
    priority: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OwnerTicketResult:
    code: str
    ticket: OwnerTicketDTO | None = None
    tickets: tuple[OwnerTicketDTO, ...] = ()

    def __post_init__(self):
        if self.code not in OWNER_TICKET_RESULT_CODES:
            raise ValueError("Unsupported owner ticket result code.")


class OwnerTicketService:
    """Keep ORM entities and database exception details out of Telegram handlers."""

    def __init__(self, repository=None):
        self.repository = repository or SupportTicketRepository()

    @staticmethod
    def valid_ticket_number(ticket_number) -> bool:
        return isinstance(ticket_number, str) and bool(
            TICKET_NUMBER_PATTERN.fullmatch(ticket_number)
        )

    @staticmethod
    def _dto(row) -> OwnerTicketDTO:
        return OwnerTicketDTO(
            ticket_number=row.ticket_number,
            category=row.category,
            priority=row.priority,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _rollback(db) -> None:
        try:
            db.rollback()
        except Exception:
            pass

    def list_active_tickets(self, db, *, limit: int = 10) -> OwnerTicketResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            return OwnerTicketResult("invalid_argument")
        try:
            rows = self.repository.list_active_for_owner_management(db, limit=limit)
            tickets = tuple(self._dto(row) for row in rows)
            return OwnerTicketResult("success", tickets=tickets) if tickets else OwnerTicketResult("empty")
        except Exception:
            self._rollback(db)
            return OwnerTicketResult("database_error")

    def get_ticket(self, db, ticket_number: str) -> OwnerTicketResult:
        if not self.valid_ticket_number(ticket_number):
            return OwnerTicketResult("invalid_argument")
        try:
            row = self.repository.get_for_owner_management(
                db, ticket_number=ticket_number
            )
            if row is None:
                return OwnerTicketResult("not_available")
            return OwnerTicketResult("success", ticket=self._dto(row))
        except Exception:
            self._rollback(db)
            return OwnerTicketResult("database_error")

    def take_ticket(self, db, ticket_number: str) -> OwnerTicketResult:
        return self._transition(db, ticket_number, operation="take")

    def resolve_ticket(self, db, ticket_number: str) -> OwnerTicketResult:
        return self._transition(db, ticket_number, operation="resolve")

    def _transition(self, db, ticket_number: str, *, operation: str) -> OwnerTicketResult:
        if not self.valid_ticket_number(ticket_number):
            return OwnerTicketResult("invalid_argument")
        try:
            ticket = self.repository.get_for_owner_transition(
                db, ticket_number=ticket_number
            )
            if ticket is None:
                self._rollback(db)
                return OwnerTicketResult("not_available")

            if operation == "take":
                if ticket.status == "in_progress":
                    result_ticket = self._dto(ticket)
                    self._rollback(db)
                    return OwnerTicketResult("already_in_progress", ticket=result_ticket)
                if ticket.status == "resolved":
                    self._rollback(db)
                    return OwnerTicketResult("not_available")
                if ticket.status == "closed":
                    self._rollback(db)
                    return OwnerTicketResult("closed")
                if ticket.status != "open":
                    self._rollback(db)
                    return OwnerTicketResult("not_available")
                now = datetime.now(timezone.utc)
                ticket.status = "in_progress"
                ticket.updated_at = now
                ticket.resolved_at = None
            elif operation == "resolve":
                if ticket.status == "resolved":
                    result_ticket = self._dto(ticket)
                    self._rollback(db)
                    return OwnerTicketResult("already_resolved", ticket=result_ticket)
                if ticket.status == "closed":
                    self._rollback(db)
                    return OwnerTicketResult("closed")
                if ticket.status not in {"open", "in_progress"}:
                    self._rollback(db)
                    return OwnerTicketResult("not_available")
                now = datetime.now(timezone.utc)
                ticket.status = "resolved"
                ticket.updated_at = now
                ticket.resolved_at = now
            else:
                raise ValueError("Unsupported owner ticket operation.")

            result_ticket = self._dto(ticket)
            db.commit()
            return OwnerTicketResult("success", ticket=result_ticket)
        except Exception:
            self._rollback(db)
            return OwnerTicketResult("database_error")
