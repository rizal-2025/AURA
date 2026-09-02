"""Restart-safe orchestration for the isolated internal demo chat API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import secrets
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.agents.orchestrator import AgentOrchestrator
from app.agents.result import AgentTurnResult
from app.brain.memory_manager import MemoryManager
from app.core.conversation_lock_manager import (
    ConversationBusyError,
    ConversationLockManager,
)
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.customer_identity import AuthenticatedCustomer
from app.core.input_validation import (
    InputValidationError,
    normalize_chat_message,
)
from app.core.locale import tr
from app.core.memory_errors import ConversationMemoryError
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.core.unit_of_work import UnitOfWork
from app.db.models.customer import Customer
from app.db.models.demo_persistence import (
    DEMO_SAFE_CONTENT_VERSION,
    validate_utc_datetime,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
)
from app.schemas.demo_chat import (
    MAX_DEMO_CHAT_MESSAGE_CODEPOINTS,
    DemoChatHandoff,
    DemoChatReply,
    DemoChatResponse,
)
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.conversation.general_conversation import (
    GENERAL_CONVERSATION_HISTORY_MESSAGE_LIMIT,
)
from app.services.demo_chat_errors import (
    DemoChatProviderError,
    DemoChatProviderTimeoutError,
    DemoChatRequestConflictError,
    DemoChatServiceUnavailableError,
    DemoHistoryResetRequiredError,
)
from app.services.demo_reservation_mutation import (
    decode_persisted_reservation_mutation,
    encode_reservation_operation,
)
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    demo_session_service,
)


_ADVISORY_LOCK_NAMESPACE = 0x41555241
DEMO_PROVIDER_OVERALL_TIMEOUT_SECONDS = 30.0
MAX_DEMO_PROVIDER_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class _DemoAdvisoryLockLease:
    connection: Any
    lock_key: int


class DemoPostgreSQLAdvisoryLock:
    """Hold a dedicated connection lock across the core's transactions."""

    @staticmethod
    def _invalidate_and_close(connection) -> None:
        try:
            connection.invalidate()
        except BaseException:
            pass
        try:
            connection.close()
        except BaseException:
            pass

    @staticmethod
    def _key(demo_session_id: int) -> int:
        if (
            isinstance(demo_session_id, bool)
            or not isinstance(demo_session_id, int)
            or not 1 <= demo_session_id < 2**31
        ):
            raise DemoChatServiceUnavailableError()
        return (_ADVISORY_LOCK_NAMESPACE << 31) | demo_session_id

    def acquire(
        self,
        db,
        *,
        demo_session_id: int,
    ) -> _DemoAdvisoryLockLease | None:
        connection = None
        acquired = False
        try:
            if db.get_bind().dialect.name != "postgresql":
                raise DemoChatServiceUnavailableError()
            lock_key = self._key(demo_session_id)
            connection = db.get_bind().connect()
            lock_result = connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
            acquired = lock_result is True
            connection.commit()
            if not acquired:
                connection.close()
                return None
            return _DemoAdvisoryLockLease(
                connection=connection,
                lock_key=lock_key,
            )
        except BaseException as error:
            if connection is not None:
                if acquired:
                    self._invalidate_and_close(connection)
                else:
                    try:
                        connection.rollback()
                    except BaseException:
                        pass
                    try:
                        connection.close()
                    except BaseException:
                        pass
            if isinstance(error, DemoChatServiceUnavailableError):
                raise
            if not isinstance(error, Exception):
                raise
            raise DemoChatServiceUnavailableError() from None

    def release(
        self,
        _db,
        *,
        demo_session_id: int,
        lease: _DemoAdvisoryLockLease,
    ) -> None:
        if not isinstance(lease, _DemoAdvisoryLockLease):
            raise DemoChatServiceUnavailableError()
        if lease.lock_key != self._key(demo_session_id):
            self._invalidate_and_close(lease.connection)
            raise DemoChatServiceUnavailableError()
        try:
            released = lease.connection.scalar(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": lease.lock_key},
            )
            if released is not True:
                raise DemoChatServiceUnavailableError()
            lease.connection.commit()
        except BaseException as error:
            self._invalidate_and_close(lease.connection)
            if isinstance(error, DemoChatServiceUnavailableError):
                raise
            if not isinstance(error, Exception):
                raise
            raise DemoChatServiceUnavailableError() from None
        else:
            try:
                lease.connection.close()
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                raise DemoChatServiceUnavailableError() from None


class DemoSimulatedHandoffService:
    """Orchestrator-compatible handoff boundary with no production writer."""

    REQUIRED_KEY = "handoff_required"
    STATE_KEY = "handoff_state"
    _REASON_CODES = {
        "explicit_human_request": "explicit_human_request",
        "repeated_misunderstanding": "repeated_misunderstanding",
        "repeated_invalid_input": "repeated_misunderstanding",
        "ambiguous_intent": "repeated_misunderstanding",
        "customer_frustration": "internal_error",
        "internal_error": "internal_error",
    }

    def __init__(
        self,
        memory_manager: MemoryManager,
        *,
        demo_session_id: int,
        repository: DemoHandoffEventRepository | None = None,
        reference_generator: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.memory_manager = memory_manager
        self.demo_session_id = demo_session_id
        self.repository = repository or DemoHandoffEventRepository()
        self.reference_generator = (
            reference_generator
            or (lambda: f"DEMO-HO-{secrets.token_hex(4).upper()}")
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return validate_utc_datetime(self.clock())

    def is_required(self, memory_key: str) -> bool:
        return bool(
            self.memory_manager.get_session(memory_key).get(
                self.REQUIRED_KEY
            )
        )

    def get_state(self, memory_key: str) -> dict[str, Any] | None:
        state = self.memory_manager.get_session(memory_key).get(
            self.STATE_KEY
        )
        return dict(state) if isinstance(state, dict) else None

    def require_handoff(
        self,
        memory_key: str,
        category: str,
        attempt_count: int = 1,
        db=None,
        owner_customer_id=None,
    ):
        reason_code = self._REASON_CODES.get(category)
        if reason_code is None or db is None or owner_customer_id is None:
            raise DemoChatServiceUnavailableError()
        reference = self.reference_generator()
        with UnitOfWork(db) as unit:
            event = self.repository.create_simulated(
                db,
                demo_session_id=self.demo_session_id,
                reference=reference,
                reason_code=reason_code,
                created_at=self._now(),
            )
            state = {
                self.REQUIRED_KEY: True,
                "category": category,
                "reason_code": reason_code,
                "reference": event.reference,
                "status": event.status,
                "attempt_count": max(1, int(attempt_count)),
            }
            unit.commit()
        self.memory_manager.update_session(
            memory_key,
            {self.REQUIRED_KEY: True, self.STATE_KEY: state},
        )
        return dict(state)

    def restore_active_handoff(self, memory_key: str, db, owner_customer_id):
        if owner_customer_id is None:
            raise DemoChatServiceUnavailableError()
        with UnitOfWork(db) as unit:
            events = self.repository.list_latest(
                db,
                demo_session_id=self.demo_session_id,
                limit=1,
            )
            event = events[0] if events else None
            state = (
                {
                    self.REQUIRED_KEY: True,
                    "reason_code": event.reason_code,
                    "reference": event.reference,
                    "status": event.status,
                    "attempt_count": 1,
                }
                if event is not None
                else None
            )
            unit.commit()
        if state is None:
            self.clear_handoff_state(memory_key)
            return None
        self.memory_manager.update_session(
            memory_key,
            {self.REQUIRED_KEY: True, self.STATE_KEY: state},
        )
        return dict(state)

    def clear_handoff_state(self, memory_key: str) -> None:
        self.memory_manager.remove_session_keys(
            memory_key,
            {
                self.REQUIRED_KEY,
                self.STATE_KEY,
                "misunderstanding_count",
                "ambiguity_count",
                "invalid_input_context",
                "invalid_input_count",
            },
        )

    def record_misunderstanding(self, memory_key: str) -> int:
        session = self.memory_manager.get_session(memory_key)
        session["misunderstanding_count"] = (
            int(session.get("misunderstanding_count") or 0) + 1
        )
        return session["misunderstanding_count"]

    def reset_misunderstandings(self, memory_key: str) -> None:
        self.memory_manager.get_session(memory_key)[
            "misunderstanding_count"
        ] = 0

    def record_ambiguity(self, memory_key: str) -> int:
        session = self.memory_manager.get_session(memory_key)
        session["ambiguity_count"] = (
            int(session.get("ambiguity_count") or 0) + 1
        )
        return session["ambiguity_count"]

    def reset_ambiguity(self, memory_key: str) -> None:
        self.memory_manager.get_session(memory_key)["ambiguity_count"] = 0

    def record_invalid_input(
        self,
        memory_key: str,
        workflow: str,
        stage: str | None,
    ) -> int:
        session = self.memory_manager.get_session(memory_key)
        context = f"{workflow}:{stage or 'unknown'}"
        if session.get("invalid_input_context") != context:
            session["invalid_input_context"] = context
            session["invalid_input_count"] = 0
        session["invalid_input_count"] = (
            int(session.get("invalid_input_count") or 0) + 1
        )
        return session["invalid_input_count"]

    def reset_invalid_input(self, memory_key: str) -> None:
        session = self.memory_manager.get_session(memory_key)
        session["invalid_input_context"] = None
        session["invalid_input_count"] = 0

    @staticmethod
    def explicit_response(_memory_key: str | None = None) -> str:
        return tr("handoff_simulated")

    @staticmethod
    def required_response(_memory_key: str | None = None) -> str:
        return tr("handoff_simulated")

    @staticmethod
    def waiting_response(_memory_key: str | None = None) -> str:
        return tr("handoff_simulated")

    @staticmethod
    def recovery_error_response() -> str:
        return tr("handoff_simulated")

    def status_response(self, memory_key: str) -> str:
        return (
            tr("handoff_simulated")
            if self.get_state(memory_key)
            else tr("handoff_none")
        )


class DemoChatService:
    def __init__(
        self,
        *,
        session_service: DemoSessionService | None = None,
        message_repository: DemoChatMessageRepository | None = None,
        handoff_repository: DemoHandoffEventRepository | None = None,
        lock_manager: ConversationLockManager | None = None,
        database_lock: DemoPostgreSQLAdvisoryLock | None = None,
        core_factory=None,
        clock: Callable[[], datetime] | None = None,
        provider_timeout_seconds: float = (
            DEMO_PROVIDER_OVERALL_TIMEOUT_SECONDS
        ),
    ):
        if (
            isinstance(provider_timeout_seconds, bool)
            or not isinstance(provider_timeout_seconds, (int, float))
            or not math.isfinite(provider_timeout_seconds)
            or provider_timeout_seconds <= 0
            or provider_timeout_seconds > MAX_DEMO_PROVIDER_TIMEOUT_SECONDS
        ):
            raise ValueError("Invalid demo provider timeout.")
        self.session_service = session_service or demo_session_service
        self.messages = message_repository or DemoChatMessageRepository()
        self.handoffs = handoff_repository or DemoHandoffEventRepository()
        self.lock_manager = lock_manager or ConversationLockManager()
        self.database_lock = database_lock or DemoPostgreSQLAdvisoryLock()
        self.core_factory = core_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.provider_timeout_seconds = float(provider_timeout_seconds)

    def _now(self) -> datetime:
        return validate_utc_datetime(self.clock())

    def _build_core(
        self,
        demo_session_id: int,
        *,
        memory_key: str | None = None,
        conversation_history=None,
        request_id: str | None = None,
    ):
        if self.core_factory is not None:
            return self.core_factory(demo_session_id)
        agent = AgentOrchestrator(provider_request_id=request_id)
        agent.handoff_service = DemoSimulatedHandoffService(
            agent.memory_manager,
            demo_session_id=demo_session_id,
            repository=self.handoffs,
            clock=self.clock,
        )
        if memory_key is not None:
            agent.seed_general_conversation_history(
                memory_key,
                conversation_history,
            )
        return AuthenticatedChatService(agent=agent)

    def _conversation_history(self, db, demo_session_id: int):
        """Load only this demo session's newest safety-versioned messages."""

        with UnitOfWork(db) as unit:
            rows = self.messages.list_latest(
                db,
                demo_session_id=demo_session_id,
                limit=GENERAL_CONVERSATION_HISTORY_MESSAGE_LIMIT,
            )
            history = [
                {"role": row.role, "content": row.content}
                for row in rows
            ]
            unit.commit()
        return history

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

    def _touch_and_identity(
        self,
        db,
        raw_session_token: str,
    ) -> tuple[int, AuthenticatedCustomer]:
        with UnitOfWork(db) as unit:
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
            ):
                session_id = None
                identity = None
            else:
                session_id = int(session.id)
                identity = AuthenticatedCustomer(
                    id=owner.id,
                    token_version=int(owner.token_version),
                    is_active=True,
                )
            unit.commit()
        if session_id is None or identity is None:
            raise DemoSessionRequiredError()
        return session_id, identity

    def _request_rows(self, db, demo_session_id: int, request_id: UUID):
        with UnitOfWork(db) as unit:
            rows = self.messages.list_by_request_id(
                db,
                demo_session_id=demo_session_id,
                request_id=request_id,
            )
            unit.commit()
        return rows

    def _completed_response(
        self,
        db,
        *,
        demo_session_id,
        message,
        rows,
    ):
        user_rows = [row for row in rows if row.role == "user"]
        assistant_rows = [row for row in rows if row.role == "assistant"]
        if len(user_rows) != 1 or len(assistant_rows) > 1:
            raise DemoChatServiceUnavailableError()
        if user_rows[0].content != message:
            raise DemoChatRequestConflictError()
        if not assistant_rows:
            # The committed marker remains fail-closed. Administrative
            # recovery and TTL policy are intentionally deferred.
            raise DemoChatRequestConflictError()
        user = user_rows[0]
        assistant = assistant_rows[0]
        if assistant.content_safety_version != DEMO_SAFE_CONTENT_VERSION:
            raise DemoHistoryResetRequiredError()
        reservation_mutation = decode_persisted_reservation_mutation(
            assistant.reservation_mutation_operation,
            assistant.reservation_mutation_reference,
        )
        with UnitOfWork(db) as unit:
            handoff = self.handoffs.get_latest_between(
                db,
                demo_session_id=demo_session_id,
                started_at=user.created_at,
                completed_at=assistant.created_at,
            )
            response = DemoChatResponse(
                reply=DemoChatReply(
                    id=assistant.id,
                    role="assistant",
                    content=assistant.content,
                    created_at=assistant.created_at,
                ),
                reservation_mutation=reservation_mutation,
                handoff=(
                    DemoChatHandoff(
                        status=handoff.status,
                    )
                    if handoff is not None
                    else None
                ),
            )
            unit.commit()
        return response

    async def _process_locked(
        self,
        db,
        *,
        raw_session_token: str,
        message: str,
        request_id: UUID,
        expected_session_id: int,
    ) -> DemoChatResponse:
        demo_session_id, customer = self._touch_and_identity(
            db,
            raw_session_token,
        )
        if demo_session_id != expected_session_id:
            raise DemoSessionRequiredError()

        rows = self._request_rows(db, demo_session_id, request_id)
        if rows:
            return self._completed_response(
                db,
                demo_session_id=demo_session_id,
                message=message,
                rows=rows,
            )

        try:
            with UnitOfWork(db) as unit:
                self.messages.append_request_message(
                    db,
                    demo_session_id=demo_session_id,
                    role="user",
                    content=message,
                    request_id=request_id,
                    created_at=self._now(),
                )
                unit.commit()
        except PersistenceOperationError as error:
            if not isinstance(error.__cause__, IntegrityError):
                raise
            rows = self._request_rows(db, demo_session_id, request_id)
            return self._completed_response(
                db,
                demo_session_id=demo_session_id,
                message=message,
                rows=rows,
            )

        session_reference = f"demo-session-{demo_session_id}"
        memory_key = build_authenticated_memory_key(
            customer.id,
            session_reference,
        )
        conversation_history = self._conversation_history(
            db,
            demo_session_id,
        )
        core = self._build_core(
            demo_session_id,
            memory_key=memory_key,
            conversation_history=conversation_history,
            request_id=str(request_id),
        )
        try:
            async with asyncio.timeout(self.provider_timeout_seconds):
                turn_result = await core.process_turn(
                    db=db,
                    customer=customer,
                    session_reference=session_reference,
                    message=message,
                )
                if type(turn_result) is not AgentTurnResult:
                    raise DemoChatServiceUnavailableError()
                reply = normalize_chat_message(turn_result.reply)
                mutation = encode_reservation_operation(
                    turn_result.reservation_operation
                )
        except TimeoutError:
            raise DemoChatProviderTimeoutError() from None
        except (
            ConversationMemoryError,
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise
        except DemoChatServiceUnavailableError:
            raise
        except Exception:
            raise DemoChatProviderError() from None

        with UnitOfWork(db) as unit:
            self.messages.append_request_message(
                db,
                demo_session_id=demo_session_id,
                role="assistant",
                content=reply,
                request_id=request_id,
                created_at=self._now(),
                reservation_mutation_operation=mutation.operation,
                reservation_mutation_reference=mutation.reference,
                content_safety_version=DEMO_SAFE_CONTENT_VERSION,
            )
            unit.commit()
        rows = self._request_rows(db, demo_session_id, request_id)
        return self._completed_response(
            db,
            demo_session_id=demo_session_id,
            message=message,
            rows=rows,
        )

    async def process(
        self,
        db,
        *,
        raw_session_token: str,
        message: str,
        request_id: UUID,
    ) -> DemoChatResponse:
        try:
            message = normalize_chat_message(message)
            if len(message) > MAX_DEMO_CHAT_MESSAGE_CODEPOINTS:
                raise InputValidationError("CHAT_MESSAGE_TOO_LONG")
        except InputValidationError:
            raise DemoChatServiceUnavailableError() from None
        try:
            demo_session_id = self._resolve_session_id(
                db,
                raw_session_token,
            )
            key = f"demo-chat-session-{demo_session_id}"
            async with self.lock_manager.hold(key):
                lease = self.database_lock.acquire(
                    db,
                    demo_session_id=demo_session_id,
                )
                if lease is None:
                    raise DemoChatRequestConflictError()
                active_error = None
                try:
                    return await self._process_locked(
                        db,
                        raw_session_token=raw_session_token,
                        message=message,
                        request_id=request_id,
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
        except (
            ConversationMemoryError,
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise DemoChatServiceUnavailableError() from None


demo_chat_service = DemoChatService()
