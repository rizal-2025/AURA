from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator
from pydantic_core import PydanticCustomError

from app.core.input_validation import (
    InputValidationError,
    normalize_reservation_name,
    validate_reservation_date,
    validate_reservation_people,
    validate_reservation_time,
)


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


class ReservationResponse(ReservationCreate):
    id: int
    status: str

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        from_attributes=True,
        hide_input_in_errors=True,
    )
