"""Caller-transaction repositories for isolated demo persistence."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import aliased

from app.core.ownership import require_owner_customer_id
from app.db.models.demo_persistence import (
    DEMO_ENVIRONMENT_SCOPE,
    DEMO_HANDOFF_STATUS,
    DemoChatMessage,
    DemoHandoffEvent,
    DemoRateLimitBucket,
    DemoSession,
    safe_demo_handoff_summary,
    validate_demo_digest,
    validate_utc_datetime,
)


def _require_internal_session_id(demo_session_id: int) -> int:
    if (
        isinstance(demo_session_id, bool)
        or not isinstance(demo_session_id, int)
        or demo_session_id < 1
    ):
        raise ValueError("A persisted demo session is required.")
    return demo_session_id


def _comparable_utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(
                "A valid persisted demo timestamp is required."
            ) from None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return validate_utc_datetime(value)


@dataclass(frozen=True)
class PersistedDemoChatMessage:
    id: int
    demo_session_id: int
    role: str
    content: str
    request_id: UUID
    created_at: datetime


class DemoSessionRepository:
    """Stage isolated demo-session changes; never commit independently."""

    def create(
        self,
        db,
        *,
        token_digest: str,
        owner_customer_id,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
        now: datetime | None = None,
    ) -> DemoSession:
        require_owner_customer_id(owner_customer_id)
        validate_demo_digest(token_digest)
        timestamp = validate_utc_datetime(now or datetime.now(timezone.utc))
        idle_expiry = validate_utc_datetime(idle_expires_at)
        absolute_expiry = validate_utc_datetime(absolute_expires_at)
        if idle_expiry > absolute_expiry:
            raise ValueError("Demo idle expiry cannot exceed absolute expiry.")
        row = DemoSession(
            token_digest=token_digest,
            owner_customer_id=owner_customer_id,
            environment_scope=DEMO_ENVIRONMENT_SCOPE,
            created_at=timestamp,
            last_seen_at=timestamp,
            idle_expires_at=idle_expiry,
            absolute_expires_at=absolute_expiry,
            revoked_at=None,
            updated_at=timestamp,
        )
        db.add(row)
        db.flush()
        return row

    def get_by_token_digest(self, db, *, token_digest: str):
        validate_demo_digest(token_digest)
        return db.execute(
            select(DemoSession).where(
                DemoSession.token_digest == token_digest,
                DemoSession.environment_scope == DEMO_ENVIRONMENT_SCOPE,
            )
        ).scalar_one_or_none()

    def get_active_by_token_digest(
        self,
        db,
        *,
        token_digest: str,
        now: datetime | None = None,
    ):
        validate_demo_digest(token_digest)
        timestamp = validate_utc_datetime(now or datetime.now(timezone.utc))
        return db.execute(
            select(DemoSession).where(
                DemoSession.token_digest == token_digest,
                DemoSession.environment_scope == DEMO_ENVIRONMENT_SCOPE,
                DemoSession.revoked_at.is_(None),
                DemoSession.idle_expires_at > timestamp,
                DemoSession.absolute_expires_at > timestamp,
            )
        ).scalar_one_or_none()

    def update_last_seen(
        self,
        db,
        *,
        demo_session_id: int,
        idle_expires_at: datetime,
        now: datetime | None = None,
    ):
        session_id = _require_internal_session_id(demo_session_id)
        timestamp = validate_utc_datetime(now or datetime.now(timezone.utc))
        idle_expiry = validate_utc_datetime(idle_expires_at)
        row = db.execute(
            select(DemoSession)
            .where(
                DemoSession.id == session_id,
                DemoSession.environment_scope == DEMO_ENVIRONMENT_SCOPE,
                DemoSession.revoked_at.is_(None),
                DemoSession.idle_expires_at > timestamp,
                DemoSession.absolute_expires_at > timestamp,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            return None
        existing_last_seen = _comparable_utc_datetime(row.last_seen_at)
        existing_idle_expiry = _comparable_utc_datetime(
            row.idle_expires_at
        )
        absolute_expiry = _comparable_utc_datetime(
            row.absolute_expires_at
        )
        capped_idle_expiry = min(idle_expiry, absolute_expiry)
        monotonic_last_seen = max(existing_last_seen, timestamp)
        monotonic_idle_expiry = max(
            existing_idle_expiry,
            capped_idle_expiry,
        )
        row.last_seen_at = monotonic_last_seen
        row.idle_expires_at = monotonic_idle_expiry
        row.updated_at = max(
            _comparable_utc_datetime(row.updated_at),
            monotonic_last_seen,
        )
        db.flush()
        return row

    def revoke(
        self,
        db,
        *,
        demo_session_id: int,
        now: datetime | None = None,
    ):
        session_id = _require_internal_session_id(demo_session_id)
        timestamp = validate_utc_datetime(now or datetime.now(timezone.utc))
        row = db.execute(
            select(DemoSession)
            .where(
                DemoSession.id == session_id,
                DemoSession.environment_scope == DEMO_ENVIRONMENT_SCOPE,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.revoked_at is None:
            row.revoked_at = timestamp
            row.updated_at = timestamp
            db.flush()
        return row

    def list_expired(
        self,
        db,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ):
        timestamp = validate_utc_datetime(now or datetime.now(timezone.utc))
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Demo cleanup limit must be positive.")
        return list(
            db.execute(
                select(DemoSession)
                .where(
                    DemoSession.environment_scope == DEMO_ENVIRONMENT_SCOPE,
                    or_(
                        DemoSession.revoked_at.is_not(None),
                        DemoSession.idle_expires_at <= timestamp,
                        DemoSession.absolute_expires_at <= timestamp,
                    ),
                )
                .order_by(
                    DemoSession.absolute_expires_at.asc(),
                    DemoSession.id.asc(),
                )
                .limit(limit)
            ).scalars()
        )

    def delete_internal_by_id(self, db, *, demo_session_id: int) -> int:
        """Delete only after callers have performed explicit child-first cleanup."""
        session_id = _require_internal_session_id(demo_session_id)
        result = db.execute(
            delete(DemoSession).where(
                DemoSession.id == session_id,
                DemoSession.environment_scope == DEMO_ENVIRONMENT_SCOPE,
            )
        )
        return int(result.rowcount or 0)


class DemoChatMessageRepository:
    """Keep every public message query scoped to one internal demo session."""

    @staticmethod
    def _public_history_filter():
        companion = aliased(DemoChatMessage)
        completed_pair = (
            select(companion.id)
            .where(
                companion.demo_session_id
                == DemoChatMessage.demo_session_id,
                companion.request_id == DemoChatMessage.request_id,
                companion.role != DemoChatMessage.role,
            )
            .exists()
        )
        return or_(
            DemoChatMessage.request_id.is_(None),
            completed_pair,
        )

    def append(
        self,
        db,
        *,
        demo_session_id: int,
        role: str,
        content: str,
        created_at: datetime | None = None,
    ) -> DemoChatMessage:
        session_id = _require_internal_session_id(demo_session_id)
        timestamp = validate_utc_datetime(
            created_at or datetime.now(timezone.utc)
        )
        row = DemoChatMessage(
            demo_session_id=session_id,
            role=role,
            content=content,
            created_at=timestamp,
        )
        db.add(row)
        db.flush()
        return row

    def append_request_message(
        self,
        db,
        *,
        demo_session_id: int,
        role: str,
        content: str,
        request_id: UUID,
        created_at: datetime | None = None,
    ) -> PersistedDemoChatMessage:
        session_id = _require_internal_session_id(demo_session_id)
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported demo message role.")
        if not isinstance(content, str) or not content:
            raise ValueError("Demo message content must be non-empty text.")
        if not isinstance(request_id, UUID):
            raise ValueError("A valid demo chat request ID is required.")
        timestamp = validate_utc_datetime(
            created_at or datetime.now(timezone.utc)
        )
        row = db.execute(
            text(
                """
                INSERT INTO demo_chat_messages (
                    demo_session_id,
                    role,
                    content,
                    request_id,
                    created_at
                )
                VALUES (
                    :demo_session_id,
                    :role,
                    :content,
                    :request_id,
                    :created_at
                )
                RETURNING
                    id,
                    demo_session_id,
                    role,
                    content,
                    request_id,
                    created_at
                """
            ),
            {
                "demo_session_id": session_id,
                "role": role,
                "content": content,
                "request_id": str(request_id),
                "created_at": timestamp,
            },
        ).mappings().one()
        return self._request_message(row)

    def list_by_request_id(
        self,
        db,
        *,
        demo_session_id: int,
        request_id: UUID,
    ) -> list[PersistedDemoChatMessage]:
        session_id = _require_internal_session_id(demo_session_id)
        if not isinstance(request_id, UUID):
            raise ValueError("A valid demo chat request ID is required.")
        rows = db.execute(
            text(
                """
                SELECT
                    id,
                    demo_session_id,
                    role,
                    content,
                    request_id,
                    created_at
                FROM demo_chat_messages
                WHERE demo_session_id = :demo_session_id
                  AND request_id = :request_id
                ORDER BY id ASC
                """
            ),
            {
                "demo_session_id": session_id,
                "request_id": str(request_id),
            },
        ).mappings()
        return [self._request_message(row) for row in rows]

    @staticmethod
    def _request_message(row) -> PersistedDemoChatMessage:
        request_id = row["request_id"]
        if isinstance(request_id, str):
            request_id = UUID(request_id)
        return PersistedDemoChatMessage(
            id=int(row["id"]),
            demo_session_id=int(row["demo_session_id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            request_id=request_id,
            created_at=_comparable_utc_datetime(row["created_at"]),
        )

    def list_latest(
        self,
        db,
        *,
        demo_session_id: int,
        limit: int = 50,
    ) -> list[DemoChatMessage]:
        session_id = _require_internal_session_id(demo_session_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("Demo history limit must be between 1 and 50.")
        newest_first = list(
            db.execute(
                select(DemoChatMessage)
                .where(
                    DemoChatMessage.demo_session_id == session_id,
                    self._public_history_filter(),
                )
                .order_by(
                    DemoChatMessage.created_at.desc(),
                    DemoChatMessage.id.desc(),
                )
                .limit(limit)
            ).scalars()
        )
        newest_first.reverse()
        return newest_first

    def delete_by_demo_session(self, db, *, demo_session_id: int) -> int:
        session_id = _require_internal_session_id(demo_session_id)
        result = db.execute(
            delete(DemoChatMessage).where(
                DemoChatMessage.demo_session_id == session_id
            )
        )
        return int(result.rowcount or 0)

    def count_by_demo_session(self, db, *, demo_session_id: int) -> int:
        session_id = _require_internal_session_id(demo_session_id)
        return int(
            db.scalar(
                select(func.count())
                .select_from(DemoChatMessage)
                .where(
                    DemoChatMessage.demo_session_id == session_id,
                    self._public_history_filter(),
                )
            )
            or 0
        )


class DemoHandoffEventRepository:
    """Persist simulated demo handoffs without production ticket dependencies."""

    def create_simulated(
        self,
        db,
        *,
        demo_session_id: int,
        reference: str,
        reason_code: str,
        created_at: datetime | None = None,
    ) -> DemoHandoffEvent:
        session_id = _require_internal_session_id(demo_session_id)
        timestamp = validate_utc_datetime(
            created_at or datetime.now(timezone.utc)
        )
        row = DemoHandoffEvent(
            demo_session_id=session_id,
            reference=reference,
            status=DEMO_HANDOFF_STATUS,
            reason_code=reason_code,
            safe_summary=safe_demo_handoff_summary(reason_code),
            created_at=timestamp,
        )
        db.add(row)
        db.flush()
        return row

    def list_latest(
        self,
        db,
        *,
        demo_session_id: int,
        limit: int = 50,
    ) -> list[DemoHandoffEvent]:
        session_id = _require_internal_session_id(demo_session_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("Demo handoff limit must be between 1 and 50.")
        newest_first = list(
            db.execute(
                select(DemoHandoffEvent)
                .where(DemoHandoffEvent.demo_session_id == session_id)
                .order_by(
                    DemoHandoffEvent.created_at.desc(),
                    DemoHandoffEvent.id.desc(),
                )
                .limit(limit)
            ).scalars()
        )
        newest_first.reverse()
        return newest_first

    def get_latest_between(
        self,
        db,
        *,
        demo_session_id: int,
        started_at: datetime,
        completed_at: datetime,
    ):
        session_id = _require_internal_session_id(demo_session_id)
        start = validate_utc_datetime(started_at)
        completion = validate_utc_datetime(completed_at)
        if start > completion:
            raise ValueError("Invalid demo handoff time window.")
        return db.execute(
            select(DemoHandoffEvent)
            .where(
                DemoHandoffEvent.demo_session_id == session_id,
                DemoHandoffEvent.created_at >= start,
                DemoHandoffEvent.created_at <= completion,
            )
            .order_by(
                DemoHandoffEvent.created_at.desc(),
                DemoHandoffEvent.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()

    def delete_by_demo_session(self, db, *, demo_session_id: int) -> int:
        session_id = _require_internal_session_id(demo_session_id)
        result = db.execute(
            delete(DemoHandoffEvent).where(
                DemoHandoffEvent.demo_session_id == session_id
            )
        )
        return int(result.rowcount or 0)


class DemoRateLimitBucketRepository:
    """Stage rate-limit buckets; atomic increment belongs to the service phase."""

    def get_bucket(
        self,
        db,
        *,
        scope_type: str,
        subject_digest: str,
        action: str,
        window_started_at: datetime,
        window_seconds: int,
        for_update: bool = False,
    ):
        validate_demo_digest(subject_digest)
        window_start = validate_utc_datetime(window_started_at)
        statement = select(DemoRateLimitBucket).where(
            DemoRateLimitBucket.scope_type == scope_type,
            DemoRateLimitBucket.subject_digest == subject_digest,
            DemoRateLimitBucket.action == action,
            DemoRateLimitBucket.window_started_at == window_start,
            DemoRateLimitBucket.window_seconds == window_seconds,
        )
        if for_update:
            statement = statement.with_for_update()
        return db.execute(statement).scalar_one_or_none()

    def create(
        self,
        db,
        *,
        scope_type: str,
        subject_digest: str,
        action: str,
        window_started_at: datetime,
        window_seconds: int,
        request_count: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> DemoRateLimitBucket:
        timestamp = validate_utc_datetime(now or datetime.now(timezone.utc))
        row = DemoRateLimitBucket(
            scope_type=scope_type,
            subject_digest=subject_digest,
            action=action,
            window_started_at=validate_utc_datetime(window_started_at),
            window_seconds=window_seconds,
            request_count=request_count,
            expires_at=validate_utc_datetime(expires_at),
            updated_at=timestamp,
        )
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update_count(
        db,
        *,
        bucket: DemoRateLimitBucket,
        request_count: int,
        now: datetime | None = None,
    ) -> DemoRateLimitBucket:
        bucket.request_count = request_count
        bucket.updated_at = validate_utc_datetime(
            now or datetime.now(timezone.utc)
        )
        db.flush()
        return bucket

    def list_expired(
        self,
        db,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[DemoRateLimitBucket]:
        timestamp = validate_utc_datetime(now or datetime.now(timezone.utc))
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Demo bucket cleanup limit must be positive.")
        return list(
            db.execute(
                select(DemoRateLimitBucket)
                .where(DemoRateLimitBucket.expires_at <= timestamp)
                .order_by(
                    DemoRateLimitBucket.expires_at.asc(),
                    DemoRateLimitBucket.id.asc(),
                )
                .limit(limit)
            ).scalars()
        )

    def delete_expired(
        self,
        db,
        *,
        now: datetime | None = None,
    ) -> int:
        timestamp = validate_utc_datetime(now or datetime.now(timezone.utc))
        result = db.execute(
            delete(DemoRateLimitBucket).where(
                DemoRateLimitBucket.expires_at <= timestamp
            ).execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)
