from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.core.input_validation import (
    InputValidationError,
    normalize_reservation_name,
    validate_reservation_date,
    validate_reservation_people,
    validate_reservation_time,
)


CanonicalReservationReference = Annotated[
    StrictStr,
    Field(
        min_length=36,
        max_length=36,
        pattern=r"^RSV_[0-9a-f]{32}$",
        description=(
            "Opaque public reservation reference. Clients must not derive "
            "meaning from its contents."
        ),
    ),
]


class ReservationCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
    )

    name: StrictStr
    people: StrictInt
    date: StrictStr
    time: StrictStr

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value):
        return cls._apply(normalize_reservation_name, value)

    @field_validator("people", mode="before")
    @classmethod
    def validate_people(cls, value):
        return cls._apply(validate_reservation_people, value)

    @field_validator("date", mode="before")
    @classmethod
    def validate_date(cls, value):
        return cls._apply(validate_reservation_date, value)

    @field_validator("time", mode="before")
    @classmethod
    def validate_time(cls, value):
        return cls._apply(validate_reservation_time, value)

    @staticmethod
    def _apply(validator, value):
        try:
            return validator(value)
        except InputValidationError as error:
            raise PydanticCustomError(error.code, error.code) from None


class ReservationUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
    )

    name: StrictStr | None = None
    people: StrictInt | None = None
    date: StrictStr | None = None
    time: StrictStr | None = None

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value):
        return ReservationCreate._apply(normalize_reservation_name, value)

    @field_validator("people", mode="before")
    @classmethod
    def validate_people(cls, value):
        return ReservationCreate._apply(validate_reservation_people, value)

    @field_validator("date", mode="before")
    @classmethod
    def validate_date(cls, value):
        return ReservationCreate._apply(validate_reservation_date, value)

    @field_validator("time", mode="before")
    @classmethod
    def validate_time(cls, value):
        return ReservationCreate._apply(validate_reservation_time, value)

    @model_validator(mode="after")
    def validate_exactly_one_field(self):
        selected = self.model_fields_set.intersection(
            {"name", "people", "date", "time"}
        )
        if len(selected) != 1:
            raise PydanticCustomError(
                "RESERVATION_UPDATE_INVALID",
                "RESERVATION_UPDATE_INVALID",
            )
        return self

    def selected_field(self) -> tuple[str, str | int]:
        field_name = next(iter(self.model_fields_set))
        value = getattr(self, field_name)
        if value is None:
            raise ValueError("Invalid validated reservation update.")
        return field_name, value


class PublicReservationResponse(BaseModel):
    reference: CanonicalReservationReference
    name: StrictStr
    people: StrictInt
    date: StrictStr
    time: StrictStr
    status: Literal["pending", "cancelled"]

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class PublicReservationListResponse(BaseModel):
    reservations: tuple[PublicReservationResponse, ...]
    count: StrictInt = Field(ge=0)

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )
