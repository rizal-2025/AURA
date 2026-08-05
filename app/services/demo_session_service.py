"""Transactional lifecycle service for isolated internal demo sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets

from app.core.unit_of_work import UnitOfWork
from app.db.models.customer import Customer
from app.db.models.demo_persistence import (
    safe_demo_handoff_summary,
    validate_utc_datetime,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
    DemoSessionRepository,
)
from app.schemas.demo_session import (
    DemoSessionCreateResponse,
    DemoSessionCurrentResponse,
    DemoSessionHandoff,
    DemoSessionMessage,
    DemoSessionSummary,
)
from app.services.demo_chat_errors import DemoHistoryResetRequiredError


DEMO_SESSION_IDLE_TIMEOUT = timedelta(hours=2)
DEMO_SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=24)
_OPAQUE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


class _SafeDemoSessionError(RuntimeError):
    code = "DEMO_SESSION_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class DemoServiceAuthRequiredError(_SafeDemoSessionError):
    code = "DEMO_SERVICE_AUTH_REQUIRED"


class DemoSessionRequiredError(_SafeDemoSessionError):
    code = "DEMO_SESSION_REQUIRED"


def generate_demo_session_token() -> str:
    """Return a URL-safe opaque token backed by 32 random bytes."""
    return secrets.token_urlsafe(32)


def validate_demo_session_token(raw_session_token: str) -> str:
    if (
        not isinstance(raw_session_token, str)
        or not _OPAQUE_TOKEN_PATTERN.fullmatch(raw_session_token)
    ):
        raise DemoSessionRequiredError()
    return raw_session_token


def digest_demo_session_token(raw_session_token: str) -> str:
    validated = validate_demo_session_token(raw_session_token)
    return hashlib.sha256(validated.encode("utf-8")).hexdigest()


def _database_utc_datetime(value: datetime) -> datetime:
    """Normalize database timestamps while keeping service DTOs UTC-aware."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return validate_utc_datetime(value)


def safe_demo_handoff_response(reason_code: str) -> tuple[str, str]:
    """Map untrusted persisted values to a fixed public reason and summary."""
    try:
        return reason_code, safe_demo_handoff_summary(reason_code)
    except ValueError:
        fallback_reason = "internal_error"
        return fallback_reason, safe_demo_handoff_summary(fallback_reason)


class DemoSessionService:
    """Create and resolve demo sessions within caller-provided DB sessions."""

    def __init__(
        self,
        *,
        session_repository: DemoSessionRepository | None = None,
        message_repository: DemoChatMessageRepository | None = None,
        handoff_repository: DemoHandoffEventRepository | None = None,
        token_generator: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions = session_repository or DemoSessionRepository()
        self.messages = message_repository or DemoChatMessageRepository()
        self.handoffs = handoff_repository or DemoHandoffEventRepository()
        self.token_generator = token_generator or generate_demo_session_token
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return validate_utc_datetime(self.clock())

    @staticmethod
    def build_session_summary(
        session,
        *,
        message_count: int,
    ) -> DemoSessionSummary:
        idle_expiry = _database_utc_datetime(session.idle_expires_at)
        absolute_expiry = _database_utc_datetime(
            session.absolute_expires_at
        )
        return DemoSessionSummary(
            expires_at=min(idle_expiry, absolute_expiry),
            idle_expires_at=idle_expiry,
            absolute_expires_at=absolute_expiry,
            message_count=message_count,
        )

    def create_session(self, db) -> DemoSessionCreateResponse:
        raw_token = validate_demo_session_token(self.token_generator())
        token_digest = digest_demo_session_token(raw_token)
        now = self._now()
        with UnitOfWork(db) as unit:
            owner = Customer()
            db.add(owner)
            db.flush()
            session = self.sessions.create(
                db,
                token_digest=token_digest,
                owner_customer_id=owner.id,
                idle_expires_at=now + DEMO_SESSION_IDLE_TIMEOUT,
                absolute_expires_at=now + DEMO_SESSION_ABSOLUTE_TIMEOUT,
                now=now,
            )
            response = DemoSessionCreateResponse(
                session_token=raw_token,
                session=self.build_session_summary(
                    session,
                    message_count=0,
                ),
            )
            unit.commit()
        return response

    def resolve_active_session(
        self,
        db,
        raw_session_token: str,
        *,
        now: datetime | None = None,
    ):
        token_digest = digest_demo_session_token(raw_session_token)
        timestamp = validate_utc_datetime(now or self._now())
        return self.sessions.get_active_by_token_digest(
            db,
            token_digest=token_digest,
            now=timestamp,
        )

    def touch_active_session(
        self,
        db,
        session,
        *,
        now: datetime | None = None,
    ):
        timestamp = validate_utc_datetime(now or self._now())
        absolute_expiry = _database_utc_datetime(
            session.absolute_expires_at
        )
        if session.absolute_expires_at.tzinfo is None:
            session.absolute_expires_at = absolute_expiry
        idle_expiry = min(
            timestamp + DEMO_SESSION_IDLE_TIMEOUT,
            absolute_expiry,
        )
        return self.sessions.update_last_seen(
            db,
            demo_session_id=session.id,
            idle_expires_at=idle_expiry,
            now=timestamp,
        )

    def get_current_session(
        self,
        db,
        raw_session_token: str,
    ) -> DemoSessionCurrentResponse:
        now = self._now()
        response = None
        history_reset_required = False
        with UnitOfWork(db) as unit:
            session = self.resolve_active_session(
                db,
                raw_session_token,
                now=now,
            )
            if session is not None and self.messages.has_unsafe_assistant_content(
                db,
                demo_session_id=session.id,
            ):
                history_reset_required = True
                session = None
            if session is not None:
                session = self.touch_active_session(
                    db,
                    session,
                    now=now,
                )
            if session is not None:
                messages = self.messages.list_latest(
                    db,
                    demo_session_id=session.id,
                    limit=50,
                )
                message_count = self.messages.count_by_demo_session(
                    db,
                    demo_session_id=session.id,
                )
                latest_handoffs = self.handoffs.list_latest(
                    db,
                    demo_session_id=session.id,
                    limit=1,
                )
                safe_handoff = (
                    safe_demo_handoff_response(
                        latest_handoffs[0].reason_code
                    )
                    if latest_handoffs
                    else None
                )
                handoff = (
                    DemoSessionHandoff(
                        status=latest_handoffs[0].status,
                        reason_code=safe_handoff[0],
                        safe_summary=safe_handoff[1],
                        created_at=_database_utc_datetime(
                            latest_handoffs[0].created_at
                        ),
                    )
                    if latest_handoffs
                    else None
                )
                response = DemoSessionCurrentResponse(
                    session=self.build_session_summary(
                        session,
                        message_count=message_count,
                    ),
                    messages=tuple(
                        DemoSessionMessage(
                            id=message.id,
                            role=message.role,
                            content=message.content,
                            created_at=_database_utc_datetime(
                                message.created_at
                            ),
                        )
                        for message in messages
                    ),
                    handoff=handoff,
                )
            unit.commit()
        if history_reset_required:
            raise DemoHistoryResetRequiredError()
        if response is None:
            raise DemoSessionRequiredError()
        return response


demo_session_service = DemoSessionService()
