"""Explicit allowlist mapping for public reservation responses."""

from pydantic import ValidationError

from app.schemas.reservation import (
    PublicReservationListResponse,
    PublicReservationResponse,
)
from app.services.reservation.dto import PersistedReservationDTO
from app.services.reservation.errors import PublicReservationContractError
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
    require_canonical_public_reference,
)


def map_public_reservation(
    value: PersistedReservationDTO,
) -> PublicReservationResponse:
    if type(value) is not PersistedReservationDTO:
        raise PublicReservationContractError()
    try:
        reference = require_canonical_public_reference(value.reference)
        return PublicReservationResponse(
            reference=reference,
            name=value.name,
            people=value.people,
            date=value.date,
            time=value.time,
            status=value.status,
        )
    except (
        AttributeError,
        PublicReservationReferenceUnavailableError,
        ValidationError,
    ):
        raise PublicReservationContractError() from None


def map_public_reservation_list(
    values: tuple[PersistedReservationDTO, ...],
    *,
    count: int,
) -> PublicReservationListResponse:
    if (
        type(values) is not tuple
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        raise PublicReservationContractError()
    try:
        reservations = tuple(map_public_reservation(value) for value in values)
        return PublicReservationListResponse(
            reservations=reservations,
            count=count,
        )
    except (TypeError, ValidationError, PublicReservationContractError):
        raise PublicReservationContractError() from None
