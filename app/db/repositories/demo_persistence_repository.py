"""Caller-transaction repositories for isolated demo persistence."""

from datetime import datetime, timezone

from sqlalchemy import delete, or_, select

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
        if idle_expiry > row.absolute_expires_at:
            raise ValueError("Demo idle expiry cannot exceed absolute expiry.")
        row.last_seen_at = timestamp
        row.idle_expires_at = idle_expiry
        row.updated_at = timestamp
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
                .where(DemoChatMessage.demo_session_id == session_id)
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
