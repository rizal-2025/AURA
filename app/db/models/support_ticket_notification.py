from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base


TELEGRAM_OWNER_CHANNEL = "telegram_owner"
VALID_NOTIFICATION_STATUSES = frozenset({"pending", "sending", "sent", "failed"})
VALID_NOTIFICATION_ERROR_CODES = frozenset({
    "timeout",
    "network_error",
    "rate_limited",
    "forbidden",
    "chat_not_found",
    "bad_request",
    "invalid_token",
    "unknown",
})


class SupportTicketNotification(Base):
    __tablename__ = "support_ticket_notifications"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_support_ticket_notifications"),
        CheckConstraint(
            "channel IN ('telegram_owner')",
            name="ck_support_ticket_notifications_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')",
            name="ck_support_ticket_notifications_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_support_ticket_notifications_attempt_count",
        ),
        UniqueConstraint(
            "support_ticket_id",
            "channel",
            name="uq_support_ticket_notifications_ticket_channel",
        ),
        Index(
            "ix_support_ticket_notifications_status_next_attempt",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_support_ticket_notifications_status_lease",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    support_ticket_id: Mapped[int] = mapped_column(
        ForeignKey(
            "support_tickets.id",
            name="fk_support_ticket_notifications_support_ticket_id",
        ),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TELEGRAM_OWNER_CHANNEL
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @validates("channel")
    def validate_channel(self, _key: str, value: str) -> str:
        if value != TELEGRAM_OWNER_CHANNEL:
            raise ValueError("Unsupported notification channel.")
        return value

    @validates("status")
    def validate_status(self, _key: str, value: str) -> str:
        if value not in VALID_NOTIFICATION_STATUSES:
            raise ValueError("Unsupported notification status.")
        return value

    @validates("attempt_count")
    def validate_attempt_count(self, _key: str, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Notification attempt count must be non-negative.")
        return value
