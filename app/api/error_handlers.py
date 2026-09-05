"""Safe HTTP error envelopes that never reflect request input."""

import math

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
from app.core.memory_errors import (
    ConversationMemoryError,
    ConversationMemoryValidationError,
    PostCommitMemoryPublicationError,
    ReservationMutationGuardError,
)
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.services.demo_session_service import (
    DemoServiceAuthRequiredError,
    DemoSessionRequiredError,
)
from app.services.demo_chat_errors import (
    DemoChatProviderError,
    DemoChatProviderTimeoutError,
    DemoChatRequestConflictError,
    DemoChatServiceUnavailableError,
    DemoHistoryResetRequiredError,
)
from app.services.demo_rate_limit_service import DemoRateLimitExceededError
from app.services.reservation.errors import (
    PastReservationDateError,
    PublicReservationContractError,
    ReservationNotFoundError,
    ReservationReferenceRequestError,
)


REQUEST_BODY_TOO_LARGE = "REQUEST_BODY_TOO_LARGE"
INVALID_REQUEST_FRAMING = "INVALID_REQUEST_FRAMING"
REQUEST_VALIDATION_FAILED = "REQUEST_VALIDATION_FAILED"
DEMO_CHAT_VALIDATION_ERROR = "VALIDATION_ERROR"
CONVERSATION_BUSY = "CONVERSATION_BUSY"

_PUBLIC_RESERVATION_DETAILS = {
    PastReservationDateError: (
        422,
        "PAST_RESERVATION_DATE",
        "That reservation date has already passed. Please choose today or a future date.",
    ),
    ReservationReferenceRequestError: (
        422,
        "INVALID_RESERVATION_REFERENCE",
        "Reservation reference is invalid.",
    ),
    ReservationNotFoundError: (
        404,
        "RESERVATION_NOT_FOUND",
        "Reservation is unavailable.",
    ),
    PublicReservationContractError: (
        503,
        "RESERVATION_REFERENCE_UNAVAILABLE",
        "Reservation data is temporarily unavailable.",
    ),
}

_DEMO_SESSION_DETAILS = {
    DemoServiceAuthRequiredError: (
        "DEMO_SERVICE_AUTH_REQUIRED",
        "Akses layanan demo tidak valid.",
    ),
    DemoSessionRequiredError: (
        "DEMO_SESSION_REQUIRED",
        "Sesi demo tidak valid atau telah kedaluwarsa.",
    ),
}

_DEMO_CHAT_DETAILS = {
    DemoHistoryResetRequiredError: (
        409,
        "DEMO_HISTORY_RESET_REQUIRED",
        "Riwayat demo lama harus direset sebelum sesi dapat dilanjutkan.",
    ),
    DemoChatRequestConflictError: (
        409,
        "REQUEST_CONFLICT",
        "Permintaan demo sedang diproses atau belum selesai.",
    ),
    DemoChatProviderError: (
        502,
        "PROVIDER_ERROR",
        "Layanan AI demo gagal memberikan respons.",
    ),
    DemoChatServiceUnavailableError: (
        503,
        "SERVICE_UNAVAILABLE",
        "Layanan demo sementara tidak tersedia.",
    ),
    DemoChatProviderTimeoutError: (
        504,
        "PROVIDER_TIMEOUT",
        "Layanan AI demo melewati batas waktu.",
    ),
}

_PERSISTENCE_DETAILS = (
    (
        PostCommitMemoryPublicationError,
        "COMMITTED_OPERATION_STATE_UNAVAILABLE",
        "The operation may already be completed. Check reservation status before retrying.",
    ),
    (
        ReservationMutationGuardError,
        "COMMITTED_OPERATION_STATE_UNAVAILABLE",
        "The operation may already be completed. Check reservation status before retrying.",
    ),
    (
        ConversationMemoryValidationError,
        "CONVERSATION_MEMORY_UNAVAILABLE",
        "Conversation state is temporarily unavailable.",
    ),
    (
        ConversationMemoryError,
        "CONVERSATION_MEMORY_UNAVAILABLE",
        "Conversation state is temporarily unavailable.",
    ),
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
_INTERNAL_DEMO_VALIDATION_PATHS = frozenset(
    {
        "/internal/demo/chat",
        "/internal/demo/reservations",
        "/internal/demo/reset",
    }
)


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
        ConversationMemoryError
        | PersistenceOperationError
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


async def public_reservation_exception_handler(
    _request: Request,
    error: (
        PastReservationDateError
        | ReservationReferenceRequestError
        | ReservationNotFoundError
        | PublicReservationContractError
    ),
) -> JSONResponse:
    status_code, code, detail = _PUBLIC_RESERVATION_DETAILS[type(error)]
    logger.info("HTTP REQUEST: status=%s code=%s", status_code, code)
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "detail": detail},
    )


async def demo_session_exception_handler(
    _request: Request,
    error: DemoServiceAuthRequiredError | DemoSessionRequiredError,
) -> JSONResponse:
    code, detail = _DEMO_SESSION_DETAILS[type(error)]
    logger.info("HTTP REQUEST: status=401 code=%s", code)
    return JSONResponse(
        status_code=401,
        content={
            "code": code,
            "detail": detail,
        },
        headers={"Cache-Control": "no-store"},
    )


async def demo_chat_exception_handler(
    _request: Request,
    error: (
        DemoChatRequestConflictError
        | DemoChatProviderError
        | DemoChatProviderTimeoutError
        | DemoChatServiceUnavailableError
    ),
) -> JSONResponse:
    status_code, code, detail = _DEMO_CHAT_DETAILS[type(error)]
    logger.info("HTTP REQUEST: status=%s code=%s", status_code, code)
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "detail": detail},
        headers={"Cache-Control": "no-store"},
    )


async def demo_rate_limit_exception_handler(
    _request: Request,
    error: DemoRateLimitExceededError,
) -> JSONResponse:
    logger.info("HTTP REQUEST: status=429 code=RATE_LIMIT_EXCEEDED")
    return JSONResponse(
        status_code=429,
        content={
            "code": "RATE_LIMIT_EXCEEDED",
            "detail": "Batas permintaan demo telah tercapai.",
        },
        headers={
            "Cache-Control": "no-store",
            "Retry-After": str(error.retry_after_seconds),
            "X-RateLimit-Limit": str(error.limit),
            "X-RateLimit-Remaining": str(error.remaining),
            "X-RateLimit-Reset": str(
                math.ceil(error.reset_at.timestamp())
            ),
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
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    safe_errors = _safe_validation_errors(error)
    is_internal_demo = request.url.path in _INTERNAL_DEMO_VALIDATION_PATHS
    response_code = (
        DEMO_CHAT_VALIDATION_ERROR
        if is_internal_demo
        else REQUEST_VALIDATION_FAILED
    )
    logger.info(
        "HTTP VALIDATION: status=422 code=%s error_count=%s",
        response_code,
        len(safe_errors),
    )
    return JSONResponse(
        status_code=422,
        content={
            "code": response_code,
            "detail": "Request validation failed.",
            "errors": safe_errors,
        },
        headers={"Cache-Control": "no-store"} if is_internal_demo else None,
    )
