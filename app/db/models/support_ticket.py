from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base


VALID_TICKET_STATUSES = frozenset({"open", "in_progress", "resolved", "closed"})
VALID_TICKET_PRIORITIES = frozenset({"low", "medium", "high", "urgent"})
ACTIVE_TICKET_STATUSES = frozenset({"open", "in_progress"})

# These summaries are intentionally operational and non-customer-specific.  They
# are the only summaries accepted by the ticket persistence path.
SAFE_TICKET_SUMMARIES = {
    "explicit_human_request": "Customer requested human assistance.",
    "repeated_misunderstanding": "Automated intent understanding failed repeatedly.",
    "repeated_invalid_input": "The active workflow received repeated invalid input.",
    "customer_frustration": "Customer reported a poor automated assistance experience.",
    "internal_error": "An internal service error prevented safe completion.",
    "ambiguous_intent": "The requested reservation action remained ambiguous.",
}


def validate_ticket_fields(*, category: str, reason_code: str, priority: str, status: str) -> None:
    """Validate the small, non-sensitive ticket domain before persistence."""
    if status not in VALID_TICKET_STATUSES:
        raise ValueError("Unsupported support ticket status.")
    if priority not in VALID_TICKET_PRIORITIES:
        raise ValueError("Unsupported support ticket priority.")
    if category not in SAFE_TICKET_SUMMARIES or reason_code != category:
        raise ValueError("Unsupported support ticket category or reason code.")


def safe_summary_for(*, category: str, reason_code: str) -> str:
    """Return an allowlisted summary; never accept caller supplied text."""
    validate_ticket_fields(
        category=category,
        reason_code=reason_code,
        priority="medium",
        status="open",
    )
    return SAFE_TICKET_SUMMARIES[category]


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        UniqueConstraint("ticket_number", name="uq_support_tickets_ticket_number"),
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name="ck_support_tickets_priority",
        ),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'resolved', 'closed')",
            name="ck_support_tickets_status",
        ),
        Index(
            "uq_support_tickets_active_owner_session",
            "owner_customer_id",
            "session_reference_hash",
            unique=True,
            postgresql_where=text("status IN ('open', 'in_progress')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    owner_customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", name="fk_support_tickets_owner_customer_id_customers"),
        nullable=False,
        index=True,
    )
    session_reference_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    safe_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @validates("priority")
    def validate_priority(self, _key: str, value: str) -> str:
        if value not in VALID_TICKET_PRIORITIES:
            raise ValueError("Unsupported support ticket priority.")
        return value

    @validates("status")
    def validate_status(self, _key: str, value: str) -> str:
        if value not in VALID_TICKET_STATUSES:
            raise ValueError("Unsupported support ticket status.")
        return value
