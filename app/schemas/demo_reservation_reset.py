"""Strict allowlist DTOs for internal demo reservation read and reset."""

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.reservation import CanonicalReservationReference
from app.schemas.demo_session import DemoSessionSummary


class _InternalDemoReservationDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        populate_by_name=True,
    )


class DemoReservationItem(_InternalDemoReservationDTO):
    model_config = ConfigDict(frozen=True)

    reservation_reference: CanonicalReservationReference = Field(
        serialization_alias="reservationReference"
    )
    status: Literal["pending", "cancelled"]
    reservation_date: date = Field(serialization_alias="reservationDate")
    reservation_time: time = Field(serialization_alias="reservationTime")
    party_size: int = Field(gt=0, serialization_alias="partySize")


class DemoReservationListResponse(_InternalDemoReservationDTO):
    reservations: tuple[DemoReservationItem, ...]
    count: int = Field(ge=0)


class DemoResetResponse(_InternalDemoReservationDTO):
    status: Literal["reset"] = "reset"
    session: DemoSessionSummary
    reservation_count: Literal[0] = Field(
        default=0,
        serialization_alias="reservationCount",
    )
    handoff: None = None
