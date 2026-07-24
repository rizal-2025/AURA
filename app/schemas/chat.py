from pydantic import BaseModel, ConfigDict, StrictStr, field_validator
from pydantic_core import PydanticCustomError

from app.core.input_validation import (
    InputValidationError,
    normalize_chat_message,
    validate_session_reference,
)

class ChatRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
    )

    session_id: StrictStr

    message: StrictStr

    @field_validator("session_id", mode="before")
    @classmethod
    def validate_session_id(cls, value):
        try:
            return validate_session_reference(value)
        except InputValidationError as error:
            raise PydanticCustomError(error.code, error.code) from None

    @field_validator("message", mode="before")
    @classmethod
    def validate_message(cls, value):
        try:
            return normalize_chat_message(value)
        except InputValidationError as error:
            raise PydanticCustomError(error.code, error.code) from None


class ChatResponse(BaseModel):
    reply: str
