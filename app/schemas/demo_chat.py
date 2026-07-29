"""Strict DTOs for the internal portfolio demo chat endpoint."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
)
from pydantic_core import PydanticCustomError

from app.core.input_validation import (
    InputValidationError,
    normalize_chat_message,
)


MAX_DEMO_CHAT_MESSAGE_CODEPOINTS = 1000


class _InternalDemoChatDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        populate_by_name=True,
    )


class DemoChatRequest(_InternalDemoChatDTO):
    message: StrictStr
    request_id: UUID = Field(alias="requestId")

    @field_validator("message", mode="before")
    @classmethod
    def validate_message(cls, value):
        try:
            normalized = normalize_chat_message(value)
        except InputValidationError as error:
            raise PydanticCustomError(error.code, error.code) from None
        if len(normalized) > MAX_DEMO_CHAT_MESSAGE_CODEPOINTS:
            raise PydanticCustomError(
                "CHAT_MESSAGE_TOO_LONG",
                "CHAT_MESSAGE_TOO_LONG",
            )
        return normalized


class DemoChatReply(_InternalDemoChatDTO):
    id: int = Field(gt=0)
    role: Literal["assistant"]
    content: str = Field(min_length=1)
    created_at: datetime = Field(serialization_alias="createdAt")


class DemoChatHandoff(_InternalDemoChatDTO):
    reference: str
    status: Literal["simulated"]


class DemoReservationMutation(_InternalDemoChatDTO):
    type: Literal["created", "updated", "cancelled"]
    reservation_id: str = Field(serialization_alias="reservationId")


class DemoChatResponse(_InternalDemoChatDTO):
    reply: DemoChatReply
    reservation_mutation: DemoReservationMutation | None = Field(
        default=None,
        serialization_alias="reservationMutation",
    )
    handoff: DemoChatHandoff | None = None
