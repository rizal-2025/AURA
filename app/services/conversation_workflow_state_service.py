"""Short-transaction persistence for restart-safe reservation workflows."""

from __future__ import annotations

from copy import deepcopy

from app.brain.reservation_workflow_snapshot import (
    WORKFLOW_SCHEMA_VERSION,
    ReservationWorkflowSnapshot,
    capture_reservation_workflow_snapshot,
    mutation_blocker_snapshot,
    validate_persisted_workflow_snapshot,
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
from app.services.handoff.ticket_service import TicketService


class ConversationWorkflowStateService:
    """Restore and publish only the strict reservation workflow projection."""

    def __init__(self, memory_manager, repository=None):
        self.memory_manager = memory_manager
        self.repository = (
            repository or ConversationWorkflowStateRepository()
        )

    @staticmethod
    def hash_session_reference(memory_key: str) -> str:
        return TicketService.hash_session_reference(memory_key)

    def restore(self, db, *, owner_customer_id, memory_key: str) -> None:
        require_owner_customer_id(owner_customer_id)
        session_hash = self.hash_session_reference(memory_key)
        try:
            with UnitOfWork(db) as unit:
                row = self.repository.get_by_scope(
                    db,
                    owner_customer_id=owner_customer_id,
                    session_reference_hash=session_hash,
                )
                if row is None:
                    stored = None
                else:
                    stored = {
                        "schema_version": row.schema_version,
                        "payload": deepcopy(row.payload),
                        "is_active": row.is_active,
                        "revision": row.revision,
                    }
                unit.commit()
        except (
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            raise
        except Exception:
            raise ConversationWorkflowRecoveryError() from None

        try:
            if stored is None:
                self.memory_manager.replace_reservation_workflow_state(
                    memory_key,
                    {},
                )
                self.memory_manager.set_workflow_persistence_revision(
                    memory_key,
                    0,
                )
                return

            if (
                type(stored["revision"]) is not int
                or stored["revision"] < 1
                or type(stored["is_active"]) is not bool
                or type(stored["schema_version"]) is not int
                or stored["schema_version"] != WORKFLOW_SCHEMA_VERSION
            ):
                raise ConversationWorkflowRecoveryError()

            if stored["is_active"]:
                snapshot = validate_persisted_workflow_snapshot(
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
                stored["revision"],
            )
        except ConversationWorkflowRecoveryError:
            raise
        except ConversationMemoryError:
            raise ConversationWorkflowRecoveryError() from None
        except Exception:
            raise ConversationWorkflowRecoveryError() from None

    def publish(self, db, *, owner_customer_id, memory_key: str) -> None:
        try:
            snapshot = capture_reservation_workflow_snapshot(
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
            current = capture_reservation_workflow_snapshot(
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
                snapshot=mutation_blocker_snapshot(operation),
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
        payload = snapshot.materialize() if snapshot is not None else {}
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
                        schema_version=WORKFLOW_SCHEMA_VERSION,
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
                    schema_version=WORKFLOW_SCHEMA_VERSION,
                    payload=payload,
                    is_active=is_active,
                )
                new_revision = row.revision
            unit.commit()

        self.memory_manager.set_workflow_persistence_revision(
            memory_key,
            new_revision,
        )
