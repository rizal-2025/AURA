"""Owner-scoped reservation read and atomic reset for the isolated demo."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.core.conversation_lock_manager import (
    ConversationBusyError,
    ConversationLockManager,
)
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.core.unit_of_work import UnitOfWork
from app.db.models.customer import Customer
from app.db.models.demo_persistence import validate_utc_datetime
from app.db.repositories.conversation_workflow_state_repository import (
    ConversationWorkflowStateRepository,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
)
from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.demo_reservation_reset import (
    DemoReservationItem,
    DemoReservationListResponse,
    DemoResetResponse,
)
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from app.services.demo_chat_errors import (
    DemoChatRequestConflictError,
    DemoChatServiceUnavailableError,
)
from app.services.demo_chat_service import (
    DemoPostgreSQLAdvisoryLock,
    demo_chat_service,
)
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    demo_session_service,
)


class DemoReservationResetService:
    def __init__(
        self,
        *,
        session_service: DemoSessionService | None = None,
        reservation_repository: ReservationRepository | None = None,
        message_repository: DemoChatMessageRepository | None = None,
        handoff_repository: DemoHandoffEventRepository | None = None,
        workflow_repository: ConversationWorkflowStateRepository | None = None,
        lock_manager: ConversationLockManager | None = None,
        database_lock: DemoPostgreSQLAdvisoryLock | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_service = session_service or demo_session_service
        self.reservations = reservation_repository or ReservationRepository()
        self.messages = message_repository or DemoChatMessageRepository()
        self.handoffs = handoff_repository or DemoHandoffEventRepository()
        self.workflows = (
            workflow_repository or ConversationWorkflowStateRepository()
        )
        self.lock_manager = lock_manager or demo_chat_service.lock_manager
        self.database_lock = database_lock or demo_chat_service.database_lock
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return validate_utc_datetime(self.clock())

    @staticmethod
    def _memory_key(demo_session_id: int) -> str:
        return f"demo-session-{demo_session_id}"

    @classmethod
    def _process_lock_key(cls, demo_session_id: int) -> str:
        return f"demo-chat-session-{demo_session_id}"

    def _resolve_session_id(self, db, raw_session_token: str) -> int:
        with UnitOfWork(db) as unit:
            session = self.session_service.resolve_active_session(
                db,
                raw_session_token,
                now=self._now(),
            )
            session_id = int(session.id) if session is not None else None
            unit.commit()
        if session_id is None:
            raise DemoSessionRequiredError()
        return session_id

    def _active_session_and_owner(
        self,
        db,
        raw_session_token: str,
        *,
        expected_session_id: int | None = None,
    ):
        now = self._now()
        session = self.session_service.resolve_active_session(
            db,
            raw_session_token,
            now=now,
        )
        if session is not None:
            session = self.session_service.touch_active_session(
                db,
                session,
                now=now,
            )
        owner = (
            db.get(Customer, session.owner_customer_id)
            if session is not None
            else None
        )
        if (
            session is None
            or owner is None
            or not bool(owner.is_active)
            or (
                expected_session_id is not None
                and int(session.id) != expected_session_id
            )
        ):
            return None, None
        return session, owner

    def list_reservations(
        self,
        db,
        *,
        raw_session_token: str,
    ) -> DemoReservationListResponse:
        try:
            result = None
            with UnitOfWork(db) as unit:
                _session, owner = self._active_session_and_owner(
                    db,
                    raw_session_token,
                )
                if owner is not None:
                    rows = self.reservations.list_for_owner(
                        db,
                        owner_customer_id=owner.id,
                        limit=50,
                    )
                    count = self.reservations.count_for_owner(
                        db,
                        owner_customer_id=owner.id,
                    )
                    result = DemoReservationListResponse(
                        reservations=tuple(
                            DemoReservationItem(
                                reservation_reference=row.public_reference,
                                status=str(row.status).lower(),
                                reservation_date=row.date,
                                reservation_time=row.time,
                                party_size=row.people,
                            )
                            for row in rows
                        ),
                        count=count,
                    )
                unit.commit()
            if result is None:
                raise DemoSessionRequiredError()
            return result
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

    def _reset_locked(
        self,
        db,
        *,
        raw_session_token: str,
        expected_session_id: int,
    ) -> DemoResetResponse:
        result = None
        with UnitOfWork(db) as unit:
            session, owner = self._active_session_and_owner(
                db,
                raw_session_token,
                expected_session_id=expected_session_id,
            )
            if session is not None and owner is not None:
                workflow_hash = (
                    ConversationWorkflowStateService.hash_session_reference(
                        self._memory_key(expected_session_id)
                    )
                )
                self.messages.delete_by_demo_session(
                    db,
                    demo_session_id=expected_session_id,
                )
                self.workflows.delete_by_scope(
                    db,
                    owner_customer_id=owner.id,
                    session_reference_hash=workflow_hash,
                )
                self.handoffs.delete_by_demo_session(
                    db,
                    demo_session_id=expected_session_id,
                )
                self.reservations.delete_by_owner_customer_id(
                    db,
                    owner_customer_id=owner.id,
                )
                result = DemoResetResponse(
                    session=self.session_service.build_session_summary(
                        session,
                        message_count=0,
                    ),
                )
            unit.commit()
        if result is None:
            raise DemoSessionRequiredError()
        return result

    async def reset(
        self,
        db,
        *,
        raw_session_token: str,
    ) -> DemoResetResponse:
        try:
            demo_session_id = self._resolve_session_id(db, raw_session_token)
            async with self.lock_manager.hold(
                self._process_lock_key(demo_session_id)
            ):
                lease = self.database_lock.acquire(
                    db,
                    demo_session_id=demo_session_id,
                )
                if lease is None:
                    raise DemoChatRequestConflictError()
                active_error = None
                try:
                    return self._reset_locked(
                        db,
                        raw_session_token=raw_session_token,
                        expected_session_id=demo_session_id,
                    )
                except BaseException as error:
                    active_error = error
                    raise
                finally:
                    try:
                        self.database_lock.release(
                            db,
                            demo_session_id=demo_session_id,
                            lease=lease,
                        )
                    except DemoChatServiceUnavailableError:
                        if active_error is None:
                            raise
        except ConversationBusyError:
            raise DemoChatRequestConflictError() from None
        except (DemoSessionRequiredError, DemoChatRequestConflictError):
            raise
        except DemoChatServiceUnavailableError:
            raise
        except (
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise DemoChatServiceUnavailableError() from None
        except Exception:
            raise DemoChatServiceUnavailableError() from None


demo_reservation_reset_service = DemoReservationResetService()
