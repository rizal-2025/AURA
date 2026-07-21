from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TelegramIdentity(Base):
    """Persistent, privacy-preserving mapping from Telegram HMAC to Customer."""

    __tablename__ = "telegram_identities"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_telegram_identities"),
        UniqueConstraint("telegram_user_key", name="uq_telegram_identities_user_key"),
        UniqueConstraint("customer_id", name="uq_telegram_identities_customer_id"),
        Index("ix_telegram_identities_user_key", "telegram_user_key"),
        Index("ix_telegram_identities_customer_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_key: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", name="fk_telegram_identities_customer_id_customers"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
