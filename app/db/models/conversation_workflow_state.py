from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ConversationWorkflowState(Base):
    __tablename__ = "conversation_workflow_states"
    __table_args__ = (
        PrimaryKeyConstraint(
            "id",
            name="pk_conversation_workflow_states",
        ),
        UniqueConstraint(
            "owner_customer_id",
            "session_reference_hash",
            name="uq_conversation_workflow_states_owner_session",
        ),
        CheckConstraint(
            "schema_version = 1",
            name="ck_conversation_workflow_states_schema_version",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_conversation_workflow_states_revision",
        ),
        CheckConstraint(
            "char_length(session_reference_hash) = 64",
            name="ck_conversation_workflow_states_session_hash_length",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_conversation_workflow_states_payload_object",
        ),
        Index(
            "ix_conversation_workflow_states_owner_customer_id",
            "owner_customer_id",
        ),
        Index(
            "ix_conversation_workflow_states_updated_at",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_customer_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "customers.id",
            name=(
                "fk_conversation_workflow_states_owner_customer_id_customers"
            ),
        ),
        nullable=False,
    )
    session_reference_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
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
