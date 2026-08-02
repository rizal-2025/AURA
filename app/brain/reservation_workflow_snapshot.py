"""Strict, bounded persistence format for unfinished reservation workflows."""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.brain.memory_manager import MemoryManager
from app.brain.reservation_memory import (
    RESERVATION_OPERATIONS,
    RESERVATION_PERSISTENCE_STATE,
    RESERVATION_PERSISTENCE_STATUSES,
    get_reservation_persistence_blocker,
)
from app.core.input_validation import (
    InputValidationError,
    validate_reservation_field,
)
from app.core.memory_errors import ConversationMemoryValidationError
from app.services.reservation.public_reference import (
    InvalidPublicReservationReferenceError,
    canonicalize_public_reference,
)


WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_SCHEMA_VERSION_V2 = 2
WORKFLOW_PAYLOAD_MAX_BYTES = 4096
MAX_RESERVATION_IDENTIFIER = (2**63) - 1
EDITABLE_FIELDS = frozenset({"name", "people", "date", "time"})
CREATE_FIELDS = ("name", "people", "date", "time")
UPDATE_STAGES = frozenset(
    {"select_reservation_id", "select_field", "input_value"}
)
CANCEL_STAGES = frozenset(
    {"select_reservation_id", "confirm_cancellation"}
)
UPDATE_STAGES_V2 = frozenset(
    {"select_reservation_reference", "select_field", "input_value"}
)
CANCEL_STAGES_V2 = frozenset(
    {"select_reservation_reference", "confirm_cancellation"}
)

_CREATE_KEYS = frozenset(
    {
        "intent",
        "name",
        "people",
        "date",
        "time",
        "completed",
        "awaiting_confirmation",
        "editing_field",
        "asked_fields",
    }
)
_UPDATE_KEYS = frozenset(
    {"update_reservation_stage", "reservation_id", "editing_field"}
)
_CANCEL_KEYS = frozenset(
    {"cancel_reservation_stage", "cancel_reservation_id"}
)
_UPDATE_KEYS_V2 = frozenset(
    {
        "update_reservation_stage",
        "reservation_reference",
        "editing_field",
    }
)
_CANCEL_KEYS_V2 = frozenset(
    {"cancel_reservation_stage", "cancel_reservation_reference"}
)
_BLOCKER_KEYS = frozenset({RESERVATION_PERSISTENCE_STATE})


class ReservationWorkflowSnapshot:
    """A detached, validated workflow payload."""

    __slots__ = ("__serialized",)

    def __init__(self, payload: dict[str, Any]):
        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError):
            raise _validation_error() from None
        object.__setattr__(
            self,
            "_ReservationWorkflowSnapshot__serialized",
            serialized,
        )

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("ReservationWorkflowSnapshot is immutable")

    def materialize(self) -> dict[str, Any]:
        return json.loads(self.__serialized)


def _validation_error() -> ConversationMemoryValidationError:
    return ConversationMemoryValidationError()


def _validate_payload_size(payload: Mapping[str, Any]) -> None:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise _validation_error() from None
    if len(encoded) > WORKFLOW_PAYLOAD_MAX_BYTES:
        raise _validation_error()


def _validate_identifier(value: object, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if (
        type(value) is not int
        or not 1 <= value <= MAX_RESERVATION_IDENTIFIER
    ):
        raise _validation_error()
    return value


def _validate_reference(
    value: object,
    *,
    optional: bool,
    canonicalize_trusted_input: bool,
) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise _validation_error()
    try:
        canonical = canonicalize_public_reference(value)
    except InvalidPublicReservationReferenceError:
        raise _validation_error() from None
    if not canonicalize_trusted_input and value != canonical:
        raise _validation_error()
    return canonical


def _validate_editing_field(value: object, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or value not in EDITABLE_FIELDS:
        raise _validation_error()
    return value


def _validate_reservation_value(field: str, value: object) -> Any:
    if value is None:
        return None
    try:
        return validate_reservation_field(field, value)
    except InputValidationError:
        raise _validation_error() from None


def _validate_asked_fields(value: object) -> list[str]:
    if type(value) is not list or len(value) > len(CREATE_FIELDS):
        raise _validation_error()
    if any(type(item) is not str or item not in CREATE_FIELDS for item in value):
        raise _validation_error()
    if len(set(value)) != len(value):
        raise _validation_error()
    expected_prefix = list(CREATE_FIELDS[: len(value)])
    if value != expected_prefix:
        raise _validation_error()
    return list(value)


def _validated_blocker(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _BLOCKER_KEYS:
        raise _validation_error()
    blocker = payload.get(RESERVATION_PERSISTENCE_STATE)
    if type(blocker) is not dict or set(blocker) != {"status", "operation"}:
        raise _validation_error()
    status = blocker.get("status")
    operation = blocker.get("operation")
    if (
        type(status) is not str
        or status not in RESERVATION_PERSISTENCE_STATUSES
        or type(operation) is not str
        or operation not in RESERVATION_OPERATIONS
    ):
        raise _validation_error()
    return {
        RESERVATION_PERSISTENCE_STATE: {
            "status": status,
            "operation": operation,
        }
    }


def _validated_create(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _CREATE_KEYS:
        raise _validation_error()
    if payload.get("intent") != "reservation":
        raise _validation_error()
    if type(payload.get("completed")) is not bool:
        raise _validation_error()
    if payload["completed"]:
        raise _validation_error()
    if type(payload.get("awaiting_confirmation")) is not bool:
        raise _validation_error()

    values = {
        field: _validate_reservation_value(field, payload.get(field))
        for field in CREATE_FIELDS
    }
    editing_field = _validate_editing_field(payload.get("editing_field"))
    asked_fields = _validate_asked_fields(payload.get("asked_fields"))
    awaiting_confirmation = payload["awaiting_confirmation"]
    if awaiting_confirmation and any(values[field] is None for field in CREATE_FIELDS):
        raise _validation_error()
    if editing_field is not None and not awaiting_confirmation:
        raise _validation_error()

    return {
        "intent": "reservation",
        **values,
        "completed": False,
        "awaiting_confirmation": awaiting_confirmation,
        "editing_field": editing_field,
        "asked_fields": asked_fields,
    }


def _validated_update(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _UPDATE_KEYS:
        raise _validation_error()
    stage = payload.get("update_reservation_stage")
    if type(stage) is not str or stage not in UPDATE_STAGES:
        raise _validation_error()
    reservation_id = _validate_identifier(
        payload.get("reservation_id"),
        optional=stage == "select_reservation_id",
    )
    editing_field = _validate_editing_field(payload.get("editing_field"))
    if stage == "select_reservation_id":
        if reservation_id is not None or editing_field is not None:
            raise _validation_error()
    elif stage == "select_field":
        if reservation_id is None or editing_field is not None:
            raise _validation_error()
    elif reservation_id is None or editing_field is None:
        raise _validation_error()
    return {
        "update_reservation_stage": stage,
        "reservation_id": reservation_id,
        "editing_field": editing_field,
    }


def _validated_cancel(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _CANCEL_KEYS:
        raise _validation_error()
    stage = payload.get("cancel_reservation_stage")
    if type(stage) is not str or stage not in CANCEL_STAGES:
        raise _validation_error()
    reservation_id = _validate_identifier(
        payload.get("cancel_reservation_id"),
        optional=stage == "select_reservation_id",
    )
    if stage == "select_reservation_id" and reservation_id is not None:
        raise _validation_error()
    if stage == "confirm_cancellation" and reservation_id is None:
        raise _validation_error()
    return {
        "cancel_reservation_stage": stage,
        "cancel_reservation_id": reservation_id,
    }


def _validated_update_v2(
    payload: Mapping[str, Any],
    *,
    canonicalize_trusted_input: bool,
) -> dict[str, Any]:
    if set(payload) != _UPDATE_KEYS_V2:
        raise _validation_error()
    stage = payload.get("update_reservation_stage")
    if type(stage) is not str or stage not in UPDATE_STAGES_V2:
        raise _validation_error()
    reservation_reference = _validate_reference(
        payload.get("reservation_reference"),
        optional=stage == "select_reservation_reference",
        canonicalize_trusted_input=canonicalize_trusted_input,
    )
    editing_field = _validate_editing_field(payload.get("editing_field"))
    if stage == "select_reservation_reference":
        if reservation_reference is not None or editing_field is not None:
            raise _validation_error()
    elif stage == "select_field":
        if reservation_reference is None or editing_field is not None:
            raise _validation_error()
    elif reservation_reference is None or editing_field is None:
        raise _validation_error()
    return {
        "update_reservation_stage": stage,
        "reservation_reference": reservation_reference,
        "editing_field": editing_field,
    }


def _validated_cancel_v2(
    payload: Mapping[str, Any],
    *,
    canonicalize_trusted_input: bool,
) -> dict[str, Any]:
    if set(payload) != _CANCEL_KEYS_V2:
        raise _validation_error()
    stage = payload.get("cancel_reservation_stage")
    if type(stage) is not str or stage not in CANCEL_STAGES_V2:
        raise _validation_error()
    reservation_reference = _validate_reference(
        payload.get("cancel_reservation_reference"),
        optional=stage == "select_reservation_reference",
        canonicalize_trusted_input=canonicalize_trusted_input,
    )
    if stage == "select_reservation_reference" and reservation_reference is not None:
        raise _validation_error()
    if stage == "confirm_cancellation" and reservation_reference is None:
        raise _validation_error()
    return {
        "cancel_reservation_stage": stage,
        "cancel_reservation_reference": reservation_reference,
    }


def _decode_v2(
    payload: object,
    *,
    schema_version: object,
    canonicalize_trusted_input: bool,
) -> ReservationWorkflowSnapshot:
    if type(schema_version) is not int or schema_version != WORKFLOW_SCHEMA_VERSION_V2:
        raise _validation_error()
    if type(payload) is not dict or not payload:
        raise _validation_error()
    _validate_payload_size(payload)

    keys = set(payload)
    if RESERVATION_PERSISTENCE_STATE in keys:
        validated = _validated_blocker(payload)
    elif "update_reservation_stage" in keys:
        validated = _validated_update_v2(
            payload,
            canonicalize_trusted_input=canonicalize_trusted_input,
        )
    elif "cancel_reservation_stage" in keys:
        validated = _validated_cancel_v2(
            payload,
            canonicalize_trusted_input=canonicalize_trusted_input,
        )
    elif "intent" in keys:
        validated = _validated_create(payload)
    else:
        raise _validation_error()

    _validate_payload_size(validated)
    return ReservationWorkflowSnapshot(validated)


def decode_workflow_snapshot_v1(
    payload: object,
    *,
    schema_version: object = WORKFLOW_SCHEMA_VERSION,
) -> ReservationWorkflowSnapshot:
    """Decode one immutable legacy snapshot without rewriting its payload."""

    if type(schema_version) is not int or schema_version != WORKFLOW_SCHEMA_VERSION:
        raise _validation_error()
    if type(payload) is not dict or not payload:
        raise _validation_error()
    _validate_payload_size(payload)

    keys = set(payload)
    if RESERVATION_PERSISTENCE_STATE in keys:
        validated = _validated_blocker(payload)
    elif "update_reservation_stage" in keys:
        validated = _validated_update(payload)
    elif "cancel_reservation_stage" in keys:
        validated = _validated_cancel(payload)
    elif "intent" in keys:
        validated = _validated_create(payload)
    else:
        raise _validation_error()

    _validate_payload_size(validated)
    return ReservationWorkflowSnapshot(validated)


def decode_workflow_snapshot_v2(
    payload: object,
    *,
    schema_version: object = WORKFLOW_SCHEMA_VERSION_V2,
) -> ReservationWorkflowSnapshot:
    """Decode a stored v2 snapshot, requiring already-canonical references."""

    return _decode_v2(
        payload,
        schema_version=schema_version,
        canonicalize_trusted_input=False,
    )


def build_workflow_snapshot_v2(payload: object) -> ReservationWorkflowSnapshot:
    """Build v2 at a trusted boundary and canonicalize a valid reference."""

    return _decode_v2(
        payload,
        schema_version=WORKFLOW_SCHEMA_VERSION_V2,
        canonicalize_trusted_input=True,
    )


def validate_persisted_workflow_snapshot_v1(
    payload: object,
    *,
    schema_version: object,
) -> ReservationWorkflowSnapshot:
    """Explicit compatibility decoder for legacy schema-v1 tests."""

    return decode_workflow_snapshot_v1(
        payload,
        schema_version=schema_version,
    )


def capture_reservation_workflow_snapshot_v2(
    memory_manager: MemoryManager,
    memory_key: str,
) -> ReservationWorkflowSnapshot | None:
    """Capture only actionable reservation state; ignore unrelated memory."""

    state = memory_manager.get_session(memory_key)
    blocker = get_reservation_persistence_blocker(
        memory_manager,
        memory_key,
        state,
    )
    if blocker is not None:
        return build_workflow_snapshot_v2(
            {RESERVATION_PERSISTENCE_STATE: blocker},
        )

    update_stage = state.get("update_reservation_stage")
    cancel_stage = state.get("cancel_reservation_stage")
    awaiting_confirmation = state.get("awaiting_confirmation", False)
    completed = state.get("completed", False)
    if type(awaiting_confirmation) is not bool or type(completed) is not bool:
        raise _validation_error()
    if update_stage is not None and cancel_stage is not None:
        raise _validation_error()
    if awaiting_confirmation and (update_stage is not None or cancel_stage is not None):
        raise _validation_error()

    if update_stage is not None:
        return build_workflow_snapshot_v2(
            {
                "update_reservation_stage": update_stage,
                "reservation_reference": state.get("reservation_reference"),
                "editing_field": state.get("editing_field"),
            },
        )
    if cancel_stage is not None:
        if state.get("editing_field") is not None:
            raise _validation_error()
        return build_workflow_snapshot_v2(
            {
                "cancel_reservation_stage": cancel_stage,
                "cancel_reservation_reference": state.get(
                    "cancel_reservation_reference"
                ),
            },
        )

    if state.get("intent") == "reservation" and not completed:
        return build_workflow_snapshot_v2(
            {
                "intent": "reservation",
                "name": state.get("name"),
                "people": state.get("people"),
                "date": state.get("date"),
                "time": state.get("time"),
                "completed": False,
                "awaiting_confirmation": awaiting_confirmation,
                "editing_field": state.get("editing_field"),
                "asked_fields": list(state.get("asked_fields") or []),
            },
        )
    if awaiting_confirmation or state.get("editing_field") is not None:
        raise _validation_error()
    return None


def mutation_blocker_snapshot_v2(operation: str) -> ReservationWorkflowSnapshot:
    if operation not in RESERVATION_OPERATIONS:
        raise _validation_error()
    return build_workflow_snapshot_v2(
        {
            RESERVATION_PERSISTENCE_STATE: {
                "status": "mutation_reconciliation_required",
                "operation": operation,
            }
        }
    )
