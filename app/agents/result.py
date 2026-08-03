"""Immutable internal results for one authenticated agent turn."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.reservation.public_reference import (
    InvalidPublicReservationReferenceError,
    canonicalize_public_reference,
)


class ReservationOperationType(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    CANCELLED = "cancelled"


@dataclass(frozen=True, repr=False)
class ReservationOperationResult:
    operation: ReservationOperationType
    reference: str

    def __post_init__(self) -> None:
        if type(self.operation) is not ReservationOperationType:
            raise ValueError("INVALID_RESERVATION_OPERATION")
        try:
            canonical = canonicalize_public_reference(self.reference)
        except InvalidPublicReservationReferenceError:
            raise ValueError("INVALID_RESERVATION_OPERATION_REFERENCE") from None
        object.__setattr__(self, "reference", canonical)

    def __repr__(self) -> str:
        return f"ReservationOperationResult(operation={self.operation.value!r})"


@dataclass(frozen=True, repr=False)
class AgentTurnResult:
    reply: str
    reservation_operation: ReservationOperationResult | None = None

    def __post_init__(self) -> None:
        if type(self.reply) is not str or not self.reply.strip():
            raise ValueError("INVALID_AGENT_REPLY")
        if (
            self.reservation_operation is not None
            and type(self.reservation_operation) is not ReservationOperationResult
        ):
            raise ValueError("INVALID_RESERVATION_OPERATION")

    def __repr__(self) -> str:
        operation = (
            self.reservation_operation.operation.value
            if self.reservation_operation is not None
            else None
        )
        return f"AgentTurnResult(operation={operation!r})"
