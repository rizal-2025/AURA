"""Bounded, lock-aware cleanup for expired internal demo persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import delete, exists, select

from app.core.conversation_lock_manager import (
    ConversationBusyError,
    ConversationLockManager,
)
from app.core.transaction_errors import PersistenceOutcomeUnknownError
from app.core.unit_of_work import UnitOfWork
from app.db.models.customer import Customer
from app.db.models.demo_persistence import DemoSession, validate_utc_datetime
from app.db.models.support_ticket import SupportTicket
from app.db.models.telegram_identity import TelegramIdentity
from app.db.repositories.conversation_workflow_state_repository import (
    ConversationWorkflowStateRepository,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
    DemoRateLimitBucketRepository,
    DemoSessionRepository,
    demo_session_rate_limit_subject,
)
from app.db.repositories.reservation_repository import ReservationRepository
from app.services.demo_chat_errors import DemoChatServiceUnavailableError
from app.services.demo_chat_service import (
    DemoPostgreSQLAdvisoryLock,
    demo_chat_service,
)


DEFAULT_DEMO_CLEANUP_BATCH_SIZE = 100
MAX_DEMO_CLEANUP_BATCH_SIZE = 500


class DemoCleanupConfigurationError(RuntimeError):
    code = "DEMO_CLEANUP_CONFIGURATION_INVALID"

    def __init__(self) -> None:
        super().__init__(self.code)


class DemoCleanupUnavailableError(RuntimeError):
    code = "DEMO_CLEANUP_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


class _UnsafeDemoOwnerReferences(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoCleanupSummary:
    scanned: int
    cleaned_sessions: int
    skipped_locked: int
    skipped_not_eligible: int
    failed_sessions: int
    deleted_expired_buckets: int


@dataclass(frozen=True)
class DemoCleanupDryRunSummary:
    eligible_sessions: int
    eligible_messages: int
    eligible_reservations: int
    eligible_workflow_states: int
    eligible_handoffs: int
    eligible_session_buckets: int
    eligible_expired_buckets: int
    blocked_sessions: int


def validate_demo_cleanup_batch_size(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_DEMO_CLEANUP_BATCH_SIZE
    ):
        raise ValueError("Demo cleanup batch size must be between 1 and 500.")
    return value


class DemoCleanupService:
    """Clean each eligible session in its own independently committed UoW."""

    def __init__(
        self,
        *,
        session_factory: Callable,
        app_env: str,
        session_repository: DemoSessionRepository | None = None,
        message_repository: DemoChatMessageRepository | None = None,
        handoff_repository: DemoHandoffEventRepository | None = None,
        rate_bucket_repository: DemoRateLimitBucketRepository | None = None,
        workflow_repository: ConversationWorkflowStateRepository | None = None,
        reservation_repository: ReservationRepository | None = None,
        lock_manager: ConversationLockManager | None = None,
        database_lock: DemoPostgreSQLAdvisoryLock | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(session_factory):
            raise ValueError("A cleanup session factory is required.")
        self.session_factory = session_factory
        self.app_env = app_env
        self.sessions = session_repository or DemoSessionRepository()
        self.messages = message_repository or DemoChatMessageRepository()
        self.handoffs = handoff_repository or DemoHandoffEventRepository()
        self.rate_buckets = (
            rate_bucket_repository or DemoRateLimitBucketRepository()
        )
        self.workflows = (
            workflow_repository or ConversationWorkflowStateRepository()
        )
        self.reservations = (
            reservation_repository or ReservationRepository()
        )
        self.lock_manager = lock_manager or demo_chat_service.lock_manager
        self.database_lock = database_lock or demo_chat_service.database_lock
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _process_lock_key(demo_session_id: int) -> str:
        return f"demo-chat-session-{demo_session_id}"

    @staticmethod
    def _close_session(db) -> None:
        try:
            if db.in_transaction():
                db.rollback()
        finally:
            db.close()

    @staticmethod
    def _owner_has_non_demo_references(db, owner_customer_id) -> bool:
        has_ticket = bool(
            db.scalar(
                select(
                    exists().where(
                        SupportTicket.owner_customer_id
                        == owner_customer_id
                    )
                )
            )
        )
        has_telegram_identity = bool(
            db.scalar(
                select(
                    exists().where(
                        TelegramIdentity.customer_id
                        == owner_customer_id
                    )
                )
            )
        )
        return has_ticket or has_telegram_identity

    def _owner_is_dedicated_demo(self, db, owner_customer_id) -> bool:
        """Fail closed unless this owner belongs to exactly one demo session."""
        return (
            self.sessions.count_by_owner(
                db,
                owner_customer_id=owner_customer_id,
            )
            == 1
        )

    def _scan(self, now: datetime, batch_size: int) -> tuple[int, ...]:
        db = self.session_factory()
        try:
            with UnitOfWork(db) as unit:
                rows = self.sessions.list_expired(
                    db,
                    now=now,
                    limit=batch_size,
                )
                identifiers = tuple(int(row.id) for row in rows)
                unit.commit()
            return identifiers
        except Exception:
            raise DemoCleanupUnavailableError() from None
        finally:
            self._close_session(db)

    def _preview(
        self,
        *,
        now: datetime,
        batch_size: int,
    ) -> DemoCleanupDryRunSummary:
        """Return bounded aggregate delete counts without changing data."""
        db = self.session_factory()
        try:
            with UnitOfWork(db) as unit:
                rows = self.sessions.list_expired(
                    db,
                    now=now,
                    limit=batch_size,
                )
                counts = {
                    "messages": 0,
                    "reservations": 0,
                    "workflow_states": 0,
                    "handoffs": 0,
                    "session_buckets": 0,
                    "blocked_sessions": 0,
                }
                for row in rows:
                    session_id = int(row.id)
                    owner_customer_id = row.owner_customer_id
                    counts["messages"] += (
                        self.messages.count_all_by_demo_session(
                            db,
                            demo_session_id=session_id,
                        )
                    )
                    counts["reservations"] += (
                        self.reservations.count_for_owner(
                            db,
                            owner_customer_id,
                        )
                    )
                    counts["workflow_states"] += (
                        self.workflows.count_by_owner(
                            db,
                            owner_customer_id=owner_customer_id,
                        )
                    )
                    counts["handoffs"] += (
                        self.handoffs.count_by_demo_session(
                            db,
                            demo_session_id=session_id,
                        )
                    )
                    counts["session_buckets"] += (
                        self.rate_buckets.count_session_subject(
                            db,
                            subject_digest=demo_session_rate_limit_subject(
                                row.token_digest
                            ),
                        )
                    )
                    counts["blocked_sessions"] += int(
                        not self._owner_is_dedicated_demo(
                            db,
                            owner_customer_id,
                        )
                        or self._owner_has_non_demo_references(
                            db,
                            owner_customer_id,
                        )
                    )
                expired_buckets = len(
                    self.rate_buckets.list_expired(
                        db,
                        now=now,
                        limit=batch_size,
                    )
                )
                unit.commit()
            return DemoCleanupDryRunSummary(
                eligible_sessions=len(rows),
                eligible_messages=counts["messages"],
                eligible_reservations=counts["reservations"],
                eligible_workflow_states=counts["workflow_states"],
                eligible_handoffs=counts["handoffs"],
                eligible_session_buckets=counts["session_buckets"],
                eligible_expired_buckets=expired_buckets,
                blocked_sessions=counts["blocked_sessions"],
            )
        except Exception:
            raise DemoCleanupUnavailableError() from None
        finally:
            self._close_session(db)

    def _delete_locked_session(
        self,
        db,
        *,
        demo_session_id: int,
        now: datetime,
    ) -> bool:
        with UnitOfWork(db) as unit:
            session = self.sessions.get_expired_by_id_for_update(
                db,
                demo_session_id=demo_session_id,
                now=now,
            )
            if session is None:
                unit.commit()
                return False
            owner_customer_id = session.owner_customer_id
            if self._owner_has_non_demo_references(
                db,
                owner_customer_id,
            ):
                raise _UnsafeDemoOwnerReferences()
            if not self._owner_is_dedicated_demo(
                db,
                owner_customer_id,
            ):
                raise _UnsafeDemoOwnerReferences()
            self.messages.delete_by_demo_session(
                db,
                demo_session_id=demo_session_id,
            )
            self.workflows.delete_by_owner(
                db,
                owner_customer_id=owner_customer_id,
            )
            self.handoffs.delete_by_demo_session(
                db,
                demo_session_id=demo_session_id,
            )
            self.reservations.delete_by_owner_customer_id(
                db,
                owner_customer_id=owner_customer_id,
            )
            self.rate_buckets.delete_session_subject(
                db,
                subject_digest=demo_session_rate_limit_subject(
                    session.token_digest
                ),
            )
            if (
                self.sessions.delete_internal_by_id(
                    db,
                    demo_session_id=demo_session_id,
                )
                != 1
            ):
                raise DemoCleanupUnavailableError()
            deleted_owner = db.execute(
                delete(Customer).where(
                    Customer.id == owner_customer_id
                )
            )
            if int(deleted_owner.rowcount or 0) != 1:
                raise DemoCleanupUnavailableError()
            unit.commit()
        return True

    def _reconcile_unknown_commit(self, demo_session_id: int) -> str:
        """Classify an indeterminate commit using a fresh database session.

        An absent DemoSession proves that the atomic owner cleanup committed,
        while a present row is conservatively reported as failed. If the
        verification itself is unavailable, the result remains failed rather
        than claiming either rollback or cleanup success.
        """
        verification_db = self.session_factory()
        try:
            with UnitOfWork(verification_db) as unit:
                session_exists = (
                    verification_db.get(DemoSession, demo_session_id)
                    is not None
                )
                unit.commit()
            return "failed" if session_exists else "cleaned"
        except Exception:
            return "failed"
        finally:
            try:
                self._close_session(verification_db)
            except Exception:
                pass

    async def _clean_one(
        self,
        *,
        demo_session_id: int,
        now: datetime,
    ) -> str:
        try:
            async with self.lock_manager.hold(
                self._process_lock_key(demo_session_id)
            ):
                db = self.session_factory()
                lease = None
                outcome = "failed"
                try:
                    lease = self.database_lock.acquire(
                        db,
                        demo_session_id=demo_session_id,
                    )
                    if lease is None:
                        outcome = "locked"
                    else:
                        outcome = (
                            "cleaned"
                            if self._delete_locked_session(
                                db,
                                demo_session_id=demo_session_id,
                                now=now,
                            )
                            else "not_eligible"
                        )
                except PersistenceOutcomeUnknownError:
                    outcome = self._reconcile_unknown_commit(
                        demo_session_id
                    )
                except _UnsafeDemoOwnerReferences:
                    outcome = "failed"
                except Exception:
                    outcome = "failed"
                finally:
                    if lease is not None:
                        try:
                            self.database_lock.release(
                                db,
                                demo_session_id=demo_session_id,
                                lease=lease,
                            )
                        except DemoChatServiceUnavailableError:
                            # The cleanup transaction has already reached its
                            # own outcome. Advisory-unlock uncertainty is
                            # handled by invalidating the lock connection and
                            # must not relabel committed cleanup as failed.
                            pass
                    try:
                        self._close_session(db)
                    except Exception:
                        # Session disposal cannot reverse a committed cleanup.
                        pass
                return outcome
        except ConversationBusyError:
            return "locked"

    def _delete_expired_buckets(
        self,
        *,
        now: datetime,
        batch_size: int,
    ) -> int:
        db = self.session_factory()
        try:
            with UnitOfWork(db) as unit:
                deleted = self.rate_buckets.delete_expired_batch(
                    db,
                    now=now,
                    limit=batch_size,
                )
                unit.commit()
            return deleted
        except Exception:
            raise DemoCleanupUnavailableError() from None
        finally:
            self._close_session(db)

    async def run_once(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = DEFAULT_DEMO_CLEANUP_BATCH_SIZE,
    ) -> DemoCleanupSummary:
        if self.app_env != "demo":
            raise DemoCleanupConfigurationError()
        limit = validate_demo_cleanup_batch_size(batch_size)
        timestamp = validate_utc_datetime(now or self.clock())
        identifiers = self._scan(timestamp, limit)
        counts = {
            "cleaned": 0,
            "locked": 0,
            "not_eligible": 0,
            "failed": 0,
        }
        for demo_session_id in identifiers:
            outcome = await self._clean_one(
                demo_session_id=demo_session_id,
                now=timestamp,
            )
            counts[outcome] += 1
        deleted_buckets = self._delete_expired_buckets(
            now=timestamp,
            batch_size=limit,
        )
        return DemoCleanupSummary(
            scanned=len(identifiers),
            cleaned_sessions=counts["cleaned"],
            skipped_locked=counts["locked"],
            skipped_not_eligible=counts["not_eligible"],
            failed_sessions=counts["failed"],
            deleted_expired_buckets=deleted_buckets,
        )

    def dry_run_once(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = DEFAULT_DEMO_CLEANUP_BATCH_SIZE,
    ) -> DemoCleanupDryRunSummary:
        """Preview the exact bounded eligibility scope without mutations."""
        if self.app_env != "demo":
            raise DemoCleanupConfigurationError()
        limit = validate_demo_cleanup_batch_size(batch_size)
        timestamp = validate_utc_datetime(now or self.clock())
        return self._preview(now=timestamp, batch_size=limit)
