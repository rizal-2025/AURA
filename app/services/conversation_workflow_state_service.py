"""Short-transaction persistence for restart-safe reservation workflows."""

from __future__ import annotations

from copy import deepcopy
from enum import Enum
import re

from app.brain.reservation_workflow_snapshot import (
    WORKFLOW_SCHEMA_VERSION,
    WORKFLOW_SCHEMA_VERSION_V2,
    ReservationWorkflowSnapshot,
    build_workflow_snapshot_v2,
    capture_reservation_workflow_snapshot_v2,
    decode_workflow_snapshot_v1,
    decode_workflow_snapshot_v2,
    mutation_blocker_snapshot_v2,
)
from app.core.memory_errors import (
    ConversationMemoryError,
    ConversationWorkflowPublicationError,
    ConversationWorkflowRecoveryError,
)
from app.core.ownership import require_owner_customer_id
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.core.unit_of_work import UnitOfWork
from app.db.repositories.conversation_workflow_state_repository import (
    ConversationWorkflowStateRepository,
)
from app.db.repositories.reservation_repository import ReservationRepository
from app.services.handoff.ticket_service import TicketService
from app.services.reservation.public_reference import (
    InvalidPublicReservationReferenceError,
    canonicalize_public_reference,
)


class WorkflowV1ConversionOutcome(str, Enum):
    CONVERTED = "converted"
    ALREADY_V2 = "already_v2"
    UNAVAILABLE = "unavailable"
    REVISION_CONFLICT = "revision_conflict"


class WorkflowRestoreOutcome(str, Enum):
    RESTORED = "restored"
    EMPTY = "empty"
    LEGACY_UNAVAILABLE = "legacy_unavailable"


class _WorkflowConversionRecoverySignal(PersistenceOperationError):
    """Preserve rollback semantics while carrying no workflow data."""


_CONVERSION_MEMORY_KEY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}"
)


def _validate_conversion_memory_key(memory_key: object) -> str:
    if (
        type(memory_key) is not str
        or _CONVERSION_MEMORY_KEY_PATTERN.fullmatch(memory_key) is None
    ):
        raise ConversationWorkflowRecoveryError()
    return memory_key


class ConversationWorkflowStateService:
    """Restore and publish only the strict reservation workflow projection."""

    def __init__(
        self,
        memory_manager,
        repository=None,
        reservation_repository=None,
    ):
        self.memory_manager = memory_manager
        self.repository = (
            repository or ConversationWorkflowStateRepository()
        )
        self.reservation_repository = (
            reservation_repository or ReservationRepository()
        )

    @staticmethod
    def hash_session_reference(memory_key: str) -> str:
        return TicketService.hash_session_reference(memory_key)

    def _read_stored_state(
        self,
        db,
        *,
        owner_customer_id,
        memory_key: str,
    ) -> dict | None:
        require_owner_customer_id(owner_customer_id)
        session_hash = self.hash_session_reference(memory_key)
        try:
            with UnitOfWork(db) as unit:
                row = self.repository.get_by_scope(
                    db,
                    owner_customer_id=owner_customer_id,
                    session_reference_hash=session_hash,
                )
                stored = (
                    None
                    if row is None
                    else {
                        "schema_version": row.schema_version,
                        "payload": deepcopy(row.payload),
                        "is_active": row.is_active,
                        "revision": row.revision,
                    }
                )
                unit.commit()
            return stored
        except (
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise
        except Exception:
            raise ConversationWorkflowRecoveryError() from None

    @staticmethod
    def _validate_stored_envelope(stored: dict) -> None:
        if (
            type(stored) is not dict
            or set(stored)
            != {"schema_version", "payload", "is_active", "revision"}
            or type(stored["revision"]) is not int
            or stored["revision"] < 1
            or type(stored["is_active"]) is not bool
            or type(stored["schema_version"]) is not int
        ):
            raise ConversationWorkflowRecoveryError()

    def restore(
        self,
        db,
        *,
        owner_customer_id,
        memory_key: str,
    ) -> WorkflowRestoreOutcome:
        stored = self._read_stored_state(
            db,
            owner_customer_id=owner_customer_id,
            memory_key=memory_key,
        )
        legacy_unavailable = False
        try:
            if stored is None:
                restored_state = {}
                revision = 0
            else:
                self._validate_stored_envelope(stored)
                if stored["schema_version"] == WORKFLOW_SCHEMA_VERSION:
                    conversion = self.convert_v1_state_to_v2(
                        db,
                        owner_customer_id=owner_customer_id,
                        memory_key=memory_key,
                        expected_revision=stored["revision"],
                    )
                    if conversion is WorkflowV1ConversionOutcome.REVISION_CONFLICT:
                        stored = self._read_stored_state(
                            db,
                            owner_customer_id=owner_customer_id,
                            memory_key=memory_key,
                        )
                        if stored is None:
                            legacy_unavailable = True
                        else:
                            self._validate_stored_envelope(stored)
                            if stored["schema_version"] == WORKFLOW_SCHEMA_VERSION:
                                conversion = self.convert_v1_state_to_v2(
                                    db,
                                    owner_customer_id=owner_customer_id,
                                    memory_key=memory_key,
                                    expected_revision=stored["revision"],
                                )
                                if (
                                    conversion
                                    is WorkflowV1ConversionOutcome.REVISION_CONFLICT
                                ):
                                    raise ConversationWorkflowRecoveryError()
                            elif (
                                stored["schema_version"]
                                != WORKFLOW_SCHEMA_VERSION_V2
                            ):
                                raise ConversationWorkflowRecoveryError()
                    legacy_unavailable = (
                        legacy_unavailable
                        or conversion is WorkflowV1ConversionOutcome.UNAVAILABLE
                    )
                    stored = self._read_stored_state(
                        db,
                        owner_customer_id=owner_customer_id,
                        memory_key=memory_key,
                    )
                    if stored is None:
                        restored_state = {}
                        revision = 0
                        legacy_unavailable = True
                    else:
                        self._validate_stored_envelope(stored)

                if stored is not None:
                    if stored["schema_version"] != WORKFLOW_SCHEMA_VERSION_V2:
                        raise ConversationWorkflowRecoveryError()
                    revision = stored["revision"]
                    if stored["is_active"]:
                        snapshot = decode_workflow_snapshot_v2(
                            stored["payload"],
                            schema_version=stored["schema_version"],
                        )
                        restored_state = snapshot.materialize()
                    else:
                        if type(stored["payload"]) is not dict or stored["payload"]:
                            raise ConversationWorkflowRecoveryError()
                        restored_state = {}

            self.memory_manager.replace_reservation_workflow_state(
                memory_key,
                restored_state,
            )
            self.memory_manager.set_workflow_persistence_revision(
                memory_key,
                revision,
            )
            if legacy_unavailable:
                return WorkflowRestoreOutcome.LEGACY_UNAVAILABLE
            return (
                WorkflowRestoreOutcome.RESTORED
                if restored_state
                else WorkflowRestoreOutcome.EMPTY
            )
        except ConversationWorkflowRecoveryError:
            raise
        except (
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise
        except ConversationMemoryError:
            raise ConversationWorkflowRecoveryError() from None
        except Exception:
            raise ConversationWorkflowRecoveryError() from None

    def publish(self, db, *, owner_customer_id, memory_key: str) -> None:
        try:
            snapshot = capture_reservation_workflow_snapshot_v2(
                self.memory_manager,
                memory_key,
            )
            self._write_snapshot(
                db,
                owner_customer_id=owner_customer_id,
                memory_key=memory_key,
                snapshot=snapshot,
            )
        except ConversationWorkflowPublicationError:
            raise
        except Exception:
            raise ConversationWorkflowPublicationError() from None

    def convert_v1_state_to_v2(
        self,
        db,
        *,
        owner_customer_id,
        memory_key: str,
        expected_revision: int,
    ) -> WorkflowV1ConversionOutcome:
        """Convert one locked legacy row without exposing numeric runtime state."""

        require_owner_customer_id(owner_customer_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ConversationWorkflowRecoveryError()
        memory_key = _validate_conversion_memory_key(memory_key)
        session_hash = self.hash_session_reference(memory_key)

        try:
            with UnitOfWork(db) as unit:
                row = self.repository.get_by_scope(
                    db,
                    owner_customer_id=owner_customer_id,
                    session_reference_hash=session_hash,
                    for_update=True,
                )
                if row is None:
                    outcome = WorkflowV1ConversionOutcome.UNAVAILABLE
                elif (
                    type(row.revision) is not int
                    or row.revision != expected_revision
                ):
                    outcome = WorkflowV1ConversionOutcome.REVISION_CONFLICT
                elif (
                    type(row.schema_version) is not int
                    or type(row.is_active) is not bool
                ):
                    raise _WorkflowConversionRecoverySignal()
                elif row.schema_version == WORKFLOW_SCHEMA_VERSION_V2:
                    self._validate_v2_row(row)
                    outcome = WorkflowV1ConversionOutcome.ALREADY_V2
                elif row.schema_version != WORKFLOW_SCHEMA_VERSION:
                    raise _WorkflowConversionRecoverySignal()
                elif not row.is_active:
                    self._validate_inactive_row(row)
                    self.repository.replace(
                        row,
                        schema_version=WORKFLOW_SCHEMA_VERSION_V2,
                        payload={},
                        is_active=False,
                    )
                    outcome = WorkflowV1ConversionOutcome.CONVERTED
                else:
                    try:
                        snapshot = decode_workflow_snapshot_v1(
                            deepcopy(row.payload),
                            schema_version=row.schema_version,
                        )
                    except ConversationMemoryError:
                        raise _WorkflowConversionRecoverySignal() from None
                    converted = self._convert_active_v1_snapshot(
                        db,
                        owner_customer_id=owner_customer_id,
                        snapshot=snapshot,
                    )
                    if converted is None:
                        self.repository.replace(
                            row,
                            schema_version=WORKFLOW_SCHEMA_VERSION_V2,
                            payload={},
                            is_active=False,
                        )
                        outcome = WorkflowV1ConversionOutcome.UNAVAILABLE
                    else:
                        self.repository.replace(
                            row,
                            schema_version=WORKFLOW_SCHEMA_VERSION_V2,
                            payload=converted.materialize(),
                            is_active=True,
                        )
                        outcome = WorkflowV1ConversionOutcome.CONVERTED
                unit.commit()
            return outcome
        except _WorkflowConversionRecoverySignal:
            raise ConversationWorkflowRecoveryError() from None
        except (
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise
        except Exception:
            raise ConversationWorkflowRecoveryError() from None

    @staticmethod
    def _validate_inactive_row(row) -> None:
        if type(row.is_active) is not bool or row.is_active:
            raise _WorkflowConversionRecoverySignal()
        if type(row.payload) is not dict or row.payload:
            raise _WorkflowConversionRecoverySignal()

    @classmethod
    def _validate_v2_row(cls, row) -> None:
        if type(row.is_active) is not bool:
            raise _WorkflowConversionRecoverySignal()
        if row.is_active:
            try:
                decode_workflow_snapshot_v2(
                    deepcopy(row.payload),
                    schema_version=row.schema_version,
                )
            except ConversationMemoryError:
                raise _WorkflowConversionRecoverySignal() from None
        else:
            cls._validate_inactive_row(row)

    def _convert_active_v1_snapshot(
        self,
        db,
        *,
        owner_customer_id,
        snapshot: ReservationWorkflowSnapshot,
    ) -> ReservationWorkflowSnapshot | None:
        payload = snapshot.materialize()
        if "update_reservation_stage" in payload:
            stage = payload["update_reservation_stage"]
            reservation_id = payload["reservation_id"]
            if stage == "select_reservation_id":
                return build_workflow_snapshot_v2(
                    {
                        "update_reservation_stage": (
                            "select_reservation_reference"
                        ),
                        "reservation_reference": None,
                        "editing_field": None,
                    }
                )
            reference = self._reference_for_legacy_selection(
                db,
                owner_customer_id=owner_customer_id,
                reservation_id=reservation_id,
            )
            if reference is None:
                return None
            return build_workflow_snapshot_v2(
                {
                    "update_reservation_stage": stage,
                    "reservation_reference": reference,
                    "editing_field": payload["editing_field"],
                }
            )

        if "cancel_reservation_stage" in payload:
            stage = payload["cancel_reservation_stage"]
            reservation_id = payload["cancel_reservation_id"]
            if stage == "select_reservation_id":
                return build_workflow_snapshot_v2(
                    {
                        "cancel_reservation_stage": (
                            "select_reservation_reference"
                        ),
                        "cancel_reservation_reference": None,
                    }
                )
            reference = self._reference_for_legacy_selection(
                db,
                owner_customer_id=owner_customer_id,
                reservation_id=reservation_id,
            )
            if reference is None:
                return None
            return build_workflow_snapshot_v2(
                {
                    "cancel_reservation_stage": stage,
                    "cancel_reservation_reference": reference,
                }
            )

        return build_workflow_snapshot_v2(payload)

    def _reference_for_legacy_selection(
        self,
        db,
        *,
        owner_customer_id,
        reservation_id: int,
    ) -> str | None:
        row = self.reservation_repository.get_by_id_for_workflow_v1_conversion(
            db,
            reservation_id,
            owner_customer_id,
        )
        if row is None:
            return None
        value = getattr(row, "public_reference", None)
        try:
            canonical = canonicalize_public_reference(value)
        except InvalidPublicReservationReferenceError:
            return None
        return canonical if value == canonical else None

    def begin_mutation(
        self,
        db,
        *,
        owner_customer_id,
        memory_key: str,
        operation: str,
    ) -> None:
        """Persist a fail-closed marker before a reservation mutation begins."""

        if not self.memory_manager.is_workflow_persistence_initialized(
            memory_key
        ):
            # Direct domain-agent callers do not own the authenticated
            # recovery/publication boundary. Production chat always restores
            # first, which initializes this scope before mutation processing.
            return
        try:
            current = capture_reservation_workflow_snapshot_v2(
                self.memory_manager,
                memory_key,
            )
            if current is None:
                raise ConversationWorkflowPublicationError()
            payload = current.materialize()
            operation_matches = (
                operation == "create"
                and payload.get("intent") == "reservation"
            ) or (
                operation == "update"
                and "update_reservation_stage" in payload
            ) or (
                operation == "cancel"
                and "cancel_reservation_stage" in payload
            )
            if not operation_matches:
                raise ConversationWorkflowPublicationError()
            self._write_snapshot(
                db,
                owner_customer_id=owner_customer_id,
                memory_key=memory_key,
                snapshot=mutation_blocker_snapshot_v2(operation),
            )
        except ConversationWorkflowPublicationError:
            raise
        except Exception:
            raise ConversationWorkflowPublicationError() from None

    def _write_snapshot(
        self,
        db,
        *,
        owner_customer_id,
        memory_key: str,
        snapshot: ReservationWorkflowSnapshot | None,
    ) -> None:
        require_owner_customer_id(owner_customer_id)
        session_hash = self.hash_session_reference(memory_key)
        expected_revision = (
            self.memory_manager.get_workflow_persistence_revision(memory_key)
        )
        if snapshot is None:
            payload = {}
        else:
            payload = decode_workflow_snapshot_v2(
                snapshot.materialize(),
                schema_version=WORKFLOW_SCHEMA_VERSION_V2,
            ).materialize()
        is_active = snapshot is not None

        with UnitOfWork(db) as unit:
            row = self.repository.get_by_scope(
                db,
                owner_customer_id=owner_customer_id,
                session_reference_hash=session_hash,
                for_update=True,
            )
            if row is None:
                if expected_revision != 0:
                    raise PersistenceOperationError()
                if not is_active:
                    new_revision = 0
                else:
                    row = self.repository.create(
                        db,
                        owner_customer_id=owner_customer_id,
                        session_reference_hash=session_hash,
                        schema_version=WORKFLOW_SCHEMA_VERSION_V2,
                        payload=payload,
                        is_active=True,
                    )
                    new_revision = row.revision
            else:
                if (
                    type(row.revision) is not int
                    or row.revision != expected_revision
                ):
                    raise PersistenceOperationError()
                self.repository.replace(
                    row,
                    schema_version=WORKFLOW_SCHEMA_VERSION_V2,
                    payload=payload,
                    is_active=is_active,
                )
                new_revision = row.revision
            unit.commit()

        self.memory_manager.set_workflow_persistence_revision(
            memory_key,
            new_revision,
        )
