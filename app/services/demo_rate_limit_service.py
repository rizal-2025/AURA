"""Typed, transactionally isolated rate limits for internal demo endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import math
from typing import Callable

from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.core.unit_of_work import UnitOfWork
from app.db.models.demo_persistence import validate_utc_datetime
from app.db.repositories.demo_persistence_repository import (
    DemoRateLimitBucketRepository,
    demo_session_rate_limit_subject,
)
from app.services.demo_chat_errors import DemoChatServiceUnavailableError
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    demo_session_service,
)


class DemoRateLimitAction(str, Enum):
    SESSION_CREATE = "session_create"
    SESSION_CURRENT = "session_current"
    CHAT = "chat"
    RESERVATIONS_READ = "reservations_read"
    RESET = "reset"


@dataclass(frozen=True)
class DemoRateLimitPolicy:
    action: DemoRateLimitAction
    scope_type: str
    limit: int
    window_seconds: int


DEMO_RATE_LIMIT_POLICIES = {
    DemoRateLimitAction.SESSION_CREATE: (
        DemoRateLimitPolicy(
            DemoRateLimitAction.SESSION_CREATE,
            "ip",
            5,
            60,
        ),
        DemoRateLimitPolicy(
            DemoRateLimitAction.SESSION_CREATE,
            "global",
            30,
            60,
        ),
    ),
    DemoRateLimitAction.SESSION_CURRENT: (
        DemoRateLimitPolicy(
            DemoRateLimitAction.SESSION_CURRENT,
            "session",
            60,
            60,
        ),
    ),
    DemoRateLimitAction.CHAT: (
        DemoRateLimitPolicy(DemoRateLimitAction.CHAT, "ip", 60, 60),
        DemoRateLimitPolicy(DemoRateLimitAction.CHAT, "session", 20, 60),
        DemoRateLimitPolicy(DemoRateLimitAction.CHAT, "global", 300, 60),
    ),
    DemoRateLimitAction.RESERVATIONS_READ: (
        DemoRateLimitPolicy(
            DemoRateLimitAction.RESERVATIONS_READ,
            "session",
            30,
            60,
        ),
    ),
    DemoRateLimitAction.RESET: (
        DemoRateLimitPolicy(DemoRateLimitAction.RESET, "session", 5, 3600),
    ),
}

_GLOBAL_SUBJECT_DIGEST = hashlib.sha256(
    b"aura:internal-demo-rate-limit:global:v1"
).hexdigest()


@dataclass(frozen=True)
class DemoRateLimitDecision:
    allowed: bool
    limit: int
    current_count: int
    remaining: int
    retry_after_seconds: int
    reset_at: datetime


class DemoRateLimitExceededError(RuntimeError):
    code = "RATE_LIMIT_EXCEEDED"

    def __init__(self, decision: DemoRateLimitDecision) -> None:
        self.limit = decision.limit
        self.remaining = decision.remaining
        self.retry_after_seconds = decision.retry_after_seconds
        self.reset_at = decision.reset_at
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class DemoRateLimitService:
    """Consume server-selected policy buckets before protected operations.

    All applicable policies charge the HTTP attempt atomically.
    """

    def __init__(
        self,
        *,
        bucket_repository: DemoRateLimitBucketRepository | None = None,
        session_service: DemoSessionService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.buckets = bucket_repository or DemoRateLimitBucketRepository()
        self.sessions = session_service or demo_session_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return validate_utc_datetime(self.clock())

    @staticmethod
    def _window(
        now: datetime,
        window_seconds: int,
    ) -> tuple[datetime, datetime]:
        timestamp = validate_utc_datetime(now)
        epoch_seconds = math.floor(timestamp.timestamp())
        window_epoch = (
            epoch_seconds // window_seconds
        ) * window_seconds
        window_start = datetime.fromtimestamp(
            window_epoch,
            tz=timezone.utc,
        )
        return window_start, window_start + timedelta(seconds=window_seconds)

    def resolve_active_session_digest(
        self,
        db,
        raw_session_token: str,
    ) -> str:
        """Resolve validity first; raw tokens never reach bucket persistence."""
        try:
            with UnitOfWork(db) as unit:
                session = self.sessions.resolve_active_session(
                    db,
                    raw_session_token,
                    now=self._now(),
                )
                token_digest = (
                    demo_session_rate_limit_subject(session.token_digest)
                    if session is not None
                    else None
                )
                unit.commit()
        except DemoSessionRequiredError:
            raise
        except (
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise DemoChatServiceUnavailableError() from None
        except Exception:
            raise DemoChatServiceUnavailableError() from None
        if token_digest is None:
            raise DemoSessionRequiredError()
        return token_digest

    @staticmethod
    def _subject_for(
        policy: DemoRateLimitPolicy,
        session_token_digest: str | None,
        client_subject_digest: str | None,
    ) -> str:
        if policy.scope_type == "global":
            return _GLOBAL_SUBJECT_DIGEST
        if policy.scope_type == "session" and session_token_digest is not None:
            return demo_session_rate_limit_subject(session_token_digest)
        if policy.scope_type == "ip" and client_subject_digest is not None:
            return client_subject_digest
        raise ValueError("A required rate-limit digest is missing.")

    def enforce(
        self,
        db,
        *,
        action: DemoRateLimitAction,
        session_token_digest: str | None = None,
        client_subject_digest: str | None = None,
    ) -> tuple[DemoRateLimitDecision, ...]:
        if not isinstance(action, DemoRateLimitAction):
            raise ValueError("A typed demo rate-limit action is required.")
        policies = DEMO_RATE_LIMIT_POLICIES[action]
        now = self._now()
        decisions: list[DemoRateLimitDecision] = []
        try:
            with UnitOfWork(db) as unit:
                for policy in policies:
                    window_start, expires_at = self._window(
                        now,
                        policy.window_seconds,
                    )
                    count = self.buckets.consume_atomic(
                        db,
                        scope_type=policy.scope_type,
                        subject_digest=self._subject_for(
                            policy,
                            session_token_digest,
                            client_subject_digest,
                        ),
                        action=policy.action.value,
                        window_started_at=window_start,
                        window_seconds=policy.window_seconds,
                        expires_at=expires_at,
                        now=now,
                    )
                    decisions.append(
                        DemoRateLimitDecision(
                            allowed=count <= policy.limit,
                            limit=policy.limit,
                            current_count=count,
                            remaining=max(0, policy.limit - count),
                            retry_after_seconds=max(
                                1,
                                math.ceil(
                                    (expires_at - now).total_seconds()
                                ),
                            ),
                            reset_at=expires_at,
                        )
                    )
                unit.commit()
        except (
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise DemoChatServiceUnavailableError() from None
        except DemoChatServiceUnavailableError:
            raise
        except Exception:
            raise DemoChatServiceUnavailableError() from None

        rejected = [decision for decision in decisions if not decision.allowed]
        if rejected:
            raise DemoRateLimitExceededError(
                max(
                    rejected,
                    key=lambda item: item.retry_after_seconds,
                )
            )
        return tuple(decisions)


demo_rate_limit_service = DemoRateLimitService()
