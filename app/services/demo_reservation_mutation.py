"""Strict public mutation mapping and persistence codec for demo chat."""

from dataclasses import dataclass

from pydantic import ValidationError

from app.agents.result import (
    ReservationOperationResult,
    ReservationOperationType,
)
from app.schemas.demo_chat import (
    DemoReservationMutation,
    DemoReservationMutationOperation,
)
from app.services.demo_chat_errors import DemoChatServiceUnavailableError
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
    require_canonical_public_reference,
)


_INTERNAL_TO_PUBLIC_OPERATION = {
    ReservationOperationType.CREATED: DemoReservationMutationOperation.CREATED,
    ReservationOperationType.UPDATED: DemoReservationMutationOperation.UPDATED,
    ReservationOperationType.CANCELLED: DemoReservationMutationOperation.CANCELLED,
}
_PERSISTED_TO_PUBLIC_OPERATION = {
    operation.value: operation for operation in DemoReservationMutationOperation
}


@dataclass(frozen=True)
class PersistedDemoReservationMutation:
    operation: str | None
    reference: str | None


def encode_reservation_operation(
    value: ReservationOperationResult | None,
) -> PersistedDemoReservationMutation:
    if value is None:
        return PersistedDemoReservationMutation(None, None)
    if type(value) is not ReservationOperationResult:
        raise DemoChatServiceUnavailableError()
    try:
        operation = _INTERNAL_TO_PUBLIC_OPERATION[value.operation]
        reference = require_canonical_public_reference(value.reference)
    except (
        KeyError,
        PublicReservationReferenceUnavailableError,
    ):
        raise DemoChatServiceUnavailableError() from None
    return PersistedDemoReservationMutation(operation.value, reference)


def decode_persisted_reservation_mutation(
    operation: str | None,
    reference: str | None,
) -> DemoReservationMutation | None:
    if operation is None and reference is None:
        return None
    if type(operation) is not str or type(reference) is not str:
        raise DemoChatServiceUnavailableError()
    try:
        public_operation = _PERSISTED_TO_PUBLIC_OPERATION[operation]
        canonical_reference = require_canonical_public_reference(reference)
        return DemoReservationMutation(
            operation=public_operation,
            reservation_reference=canonical_reference,
        )
    except (
        KeyError,
        PublicReservationReferenceUnavailableError,
        ValidationError,
    ):
        raise DemoChatServiceUnavailableError() from None
