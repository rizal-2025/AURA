"""Isolated persistence models for the public AURA demo."""

from datetime import datetime, timezone
import re
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base


DEMO_ENVIRONMENT_SCOPE = "demo"
DEMO_SESSION_DIGEST_LENGTH = 64
DEMO_HANDOFF_STATUS = "simulated"
VALID_DEMO_MESSAGE_ROLES = frozenset({"user", "assistant"})
VALID_DEMO_RATE_LIMIT_SCOPES = frozenset({"session", "ip", "global"})
SAFE_DEMO_HANDOFF_SUMMARIES = {
    "explicit_human_request": "Demo visitor requested simulated human assistance.",
    "repeated_misunderstanding": "The demo assistant could not resolve the request.",
    "internal_error": "The demo assistant could not safely complete the request.",
}

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_HANDOFF_REFERENCE_PATTERN = re.compile(r"^DEMO-HO-[A-Z0-9-]{1,48}$")


def validate_demo_digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise ValueError("A valid demo digest is required.")
    return value


def validate_utc_datetime(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("A timezone-aware demo timestamp is required.")
    return value.astimezone(timezone.utc)


def safe_demo_handoff_summary(reason_code: str) -> str:
    try:
        return SAFE_DEMO_HANDOFF_SUMMARIES[reason_code]
    except (KeyError, TypeError):
        raise ValueError("Unsupported demo handoff reason.") from None


class DemoSession(Base):
    __tablename__ = "demo_sessions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_demo_sessions"),
        UniqueConstraint(
            "token_digest",
            name="uq_demo_sessions_token_digest",
        ),
        UniqueConstraint(
            "owner_customer_id",
            name="uq_demo_sessions_owner_customer_id",
        ),
        CheckConstraint(
            "environment_scope = 'demo'",
            name="ck_demo_sessions_environment_scope",
        ),
        CheckConstraint(
            "char_length(token_digest) = 64",
            name="ck_demo_sessions_token_digest_length",
        ),
        CheckConstraint(
            "idle_expires_at <= absolute_expires_at",
            name="ck_demo_sessions_expiry_order",
        ),
        Index(
            "ix_demo_sessions_expiry",
            "idle_expires_at",
            "absolute_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_customer_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "customers.id",
            name="fk_demo_sessions_owner_customer_id_customers",
        ),
        nullable=False,
    )
    environment_scope: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DEMO_ENVIRONMENT_SCOPE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    idle_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @validates("token_digest")
    def validate_token_digest(self, _key: str, value: str) -> str:
        return validate_demo_digest(value)

    @validates("environment_scope")
    def validate_environment_scope(self, _key: str, value: str) -> str:
        if value != DEMO_ENVIRONMENT_SCOPE:
            raise ValueError("Unsupported demo environment scope.")
        return value

    @validates(
        "created_at",
        "last_seen_at",
        "idle_expires_at",
        "absolute_expires_at",
        "revoked_at",
        "updated_at",
    )
    def validate_timestamp(self, _key: str, value):
        if value is None and _key == "revoked_at":
            return None
        return validate_utc_datetime(value)


class DemoChatMessage(Base):
    __tablename__ = "demo_chat_messages"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_demo_chat_messages"),
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_demo_chat_messages_role",
        ),
        Index(
            "ix_demo_chat_messages_session_created",
            "demo_session_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, nullable=False)
    demo_session_id: Mapped[int] = mapped_column(
        ForeignKey(
            "demo_sessions.id",
            name="fk_demo_chat_messages_demo_session_id",
        ),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    @validates("role")
    def validate_role(self, _key: str, value: str) -> str:
        if value not in VALID_DEMO_MESSAGE_ROLES:
            raise ValueError("Unsupported demo message role.")
        return value

    @validates("content")
    def validate_content(self, _key: str, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Demo message content must be text.")
        return value

    @validates("created_at")
    def validate_created_at(self, _key: str, value: datetime) -> datetime:
        return validate_utc_datetime(value)


class DemoHandoffEvent(Base):
    __tablename__ = "demo_handoff_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_demo_handoff_events"),
        UniqueConstraint(
            "reference",
            name="uq_demo_handoff_events_reference",
        ),
        CheckConstraint(
            "status = 'simulated'",
            name="ck_demo_handoff_events_status",
        ),
        CheckConstraint(
            "reference LIKE 'DEMO-HO-%'",
            name="ck_demo_handoff_events_reference_prefix",
        ),
        Index(
            "ix_demo_handoff_events_session_created",
            "demo_session_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, nullable=False)
    demo_session_id: Mapped[int] = mapped_column(
        ForeignKey(
            "demo_sessions.id",
            name="fk_demo_handoff_events_demo_session_id",
        ),
        nullable=False,
    )
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DEMO_HANDOFF_STATUS,
    )
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    @validates("reference")
    def validate_reference(self, _key: str, value: str) -> str:
        if (
            not isinstance(value, str)
            or not _HANDOFF_REFERENCE_PATTERN.fullmatch(value)
        ):
            raise ValueError("Invalid demo handoff reference.")
        return value

    @validates("status")
    def validate_status(self, _key: str, value: str) -> str:
        if value != DEMO_HANDOFF_STATUS:
            raise ValueError("Unsupported demo handoff status.")
        return value

    @validates("reason_code")
    def validate_reason_code(self, _key: str, value: str) -> str:
        safe_demo_handoff_summary(value)
        return value

    @validates("safe_summary")
    def validate_safe_summary(self, _key: str, value: str | None):
        if (
            value is not None
            and value not in SAFE_DEMO_HANDOFF_SUMMARIES.values()
        ):
            raise ValueError("Unsupported demo handoff summary.")
        return value

    @validates("created_at")
    def validate_created_at(self, _key: str, value: datetime) -> datetime:
        return validate_utc_datetime(value)


class DemoRateLimitBucket(Base):
    __tablename__ = "demo_rate_limit_buckets"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_demo_rate_limit_buckets"),
        UniqueConstraint(
            "scope_type",
            "subject_digest",
            "action",
            "window_started_at",
            "window_seconds",
            name="uq_demo_rate_limit_buckets_identity",
        ),
        CheckConstraint(
            "scope_type IN ('session', 'ip', 'global')",
            name="ck_demo_rate_limit_buckets_scope_type",
        ),
        CheckConstraint(
            "char_length(subject_digest) = 64",
            name="ck_demo_rate_limit_buckets_subject_digest_length",
        ),
        CheckConstraint(
            "window_seconds > 0",
            name="ck_demo_rate_limit_buckets_window_seconds",
        ),
        CheckConstraint(
            "request_count >= 0",
            name="ck_demo_rate_limit_buckets_request_count",
        ),
        Index(
            "ix_demo_rate_limit_buckets_lookup",
            "scope_type",
            "subject_digest",
            "action",
            "window_started_at",
        ),
        Index(
            "ix_demo_rate_limit_buckets_expires_at",
            "expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    request_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @validates("scope_type")
    def validate_scope_type(self, _key: str, value: str) -> str:
        if value not in VALID_DEMO_RATE_LIMIT_SCOPES:
            raise ValueError("Unsupported demo rate-limit scope.")
        return value

    @validates("subject_digest")
    def validate_subject_digest(self, _key: str, value: str) -> str:
        return validate_demo_digest(value)

    @validates("action")
    def validate_action(self, _key: str, value: str) -> str:
        if not isinstance(value, str) or not _ACTION_PATTERN.fullmatch(value):
            raise ValueError("Invalid demo rate-limit action.")
        return value

    @validates("window_seconds")
    def validate_window_seconds(self, _key: str, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("Demo rate-limit window must be positive.")
        return value

    @validates("request_count")
    def validate_request_count(self, _key: str, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Demo rate-limit request count must be non-negative.")
        return value

    @validates(
        "window_started_at",
        "expires_at",
        "updated_at",
    )
    def validate_timestamp(self, _key: str, value: datetime) -> datetime:
        return validate_utc_datetime(value)
