"""Safe HTTP error envelopes that never reflect request input."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.conversation_lock_manager import ConversationBusyError
from app.core.input_validation import (
    CHAT_MESSAGE_EMPTY,
    CHAT_MESSAGE_INVALID,
    CHAT_MESSAGE_TOO_LONG,
    CHAT_MESSAGE_UNSAFE,
    CHAT_SESSION_ID_INVALID,
    EXTRA_FIELD_FORBIDDEN,
    INPUT_INVALID,
    REQUEST_JSON_INVALID,
    RESERVATION_DATE_INVALID,
    RESERVATION_NAME_INVALID,
    RESERVATION_PEOPLE_INVALID,
    RESERVATION_TIME_INVALID,
    SAFE_INPUT_CODES,
)
from app.core.logger import logger
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)


REQUEST_BODY_TOO_LARGE = "REQUEST_BODY_TOO_LARGE"
INVALID_REQUEST_FRAMING = "INVALID_REQUEST_FRAMING"
REQUEST_VALIDATION_FAILED = "REQUEST_VALIDATION_FAILED"
CONVERSATION_BUSY = "CONVERSATION_BUSY"

_PERSISTENCE_DETAILS = (
    (
        PersistenceOutcomeUnknownError,
        "PERSISTENCE_OUTCOME_UNKNOWN",
        "The operation status is uncertain. Please verify status before retrying.",
    ),
    (
        TransactionSessionUnusableError,
        "PERSISTENCE_SESSION_UNAVAILABLE",
        "The persistence service is temporarily unavailable.",
    ),
    (
        PersistenceOperationError,
        "PERSISTENCE_OPERATION_FAILED",
        "The operation could not be saved. Please try again safely.",
    ),
)

_KNOWN_FIELD_CODES = {
    "session_id": CHAT_SESSION_ID_INVALID,
    "message": CHAT_MESSAGE_INVALID,
    "name": RESERVATION_NAME_INVALID,
    "people": RESERVATION_PEOPLE_INVALID,
    "date": RESERVATION_DATE_INVALID,
    "time": RESERVATION_TIME_INVALID,
}
_KNOWN_FIELDS = frozenset(_KNOWN_FIELD_CODES)
_MAX_SAFE_ERRORS = 8


def request_body_too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "code": REQUEST_BODY_TOO_LARGE,
            "detail": "Request body is too large.",
        },
    )


def invalid_request_framing_response() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "code": INVALID_REQUEST_FRAMING,
            "detail": "Request framing is invalid.",
        },
    )


async def conversation_busy_exception_handler(
    _request: Request,
    _error: ConversationBusyError,
) -> JSONResponse:
    logger.info("HTTP REQUEST: status=409 code=%s", CONVERSATION_BUSY)
    return JSONResponse(
        status_code=409,
        content={
            "code": CONVERSATION_BUSY,
            "detail": "This conversation is still processing a previous message.",
        },
    )


async def transaction_exception_handler(
    _request: Request,
    error: (
        PersistenceOperationError
        | PersistenceOutcomeUnknownError
        | TransactionSessionUnusableError
    ),
) -> JSONResponse:
    code, detail = next(
        (code, detail)
        for error_type, code, detail in _PERSISTENCE_DETAILS
        if isinstance(error, error_type)
    )
    logger.info("HTTP REQUEST: status=503 code=%s", code)
    return JSONResponse(
        status_code=503,
        content={
            "code": code,
            "detail": detail,
        },
    )


def _safe_validation_errors(error: RequestValidationError) -> list[dict[str, str]]:
    safe_errors: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in error.errors():
        error_type = str(item.get("type", ""))
        location = item.get("loc", ())

        if error_type == "extra_forbidden":
            field = "body"
            code = EXTRA_FIELD_FORBIDDEN
        elif error_type == "json_invalid":
            field = "body"
            code = REQUEST_JSON_INVALID
        else:
            candidate_field = location[-1] if location else None
            field = candidate_field if candidate_field in _KNOWN_FIELDS else "body"
            if error_type in SAFE_INPUT_CODES:
                code = error_type
            else:
                code = _KNOWN_FIELD_CODES.get(field, INPUT_INVALID)

        pair = (field, code)
        if pair in seen:
            continue
        seen.add(pair)
        safe_errors.append({"field": field, "code": code})
        if len(safe_errors) >= _MAX_SAFE_ERRORS:
            break
    return safe_errors or [{"field": "body", "code": INPUT_INVALID}]


async def request_validation_exception_handler(
    _request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    safe_errors = _safe_validation_errors(error)
    logger.info(
        "HTTP VALIDATION: status=422 code=%s error_count=%s",
        REQUEST_VALIDATION_FAILED,
        len(safe_errors),
    )
    return JSONResponse(
        status_code=422,
        content={
            "code": REQUEST_VALIDATION_FAILED,
            "detail": "Request validation failed.",
            "errors": safe_errors,
        },
    )
