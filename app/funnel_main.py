"""Minimal public ASGI gateway intended exclusively for Tailscale Funnel."""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.error_handlers import (
    conversation_busy_exception_handler,
    demo_chat_exception_handler,
    demo_rate_limit_exception_handler,
    demo_session_exception_handler,
    public_reservation_exception_handler,
    request_validation_exception_handler,
    transaction_exception_handler,
)
from app.api.internal_demo_chat import router as demo_chat_router
from app.api.internal_demo_reservation_reset import (
    router as demo_reservation_reset_router,
)
from app.api.internal_demo_sessions import router as demo_session_router
from app.core.config import get_application_settings
from app.core.conversation_lock_manager import ConversationBusyError
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
from app.middleware.public_demo_concurrency import (
    PublicDemoConcurrencyMiddleware,
)
from app.middleware.request_body_limit import RequestBodyLimitMiddleware
from app.services.demo_chat_errors import (
    DemoChatProviderError,
    DemoChatProviderTimeoutError,
    DemoChatRequestConflictError,
    DemoChatServiceUnavailableError,
    DemoHistoryResetRequiredError,
)
from app.services.demo_rate_limit_service import DemoRateLimitExceededError
from app.services.demo_session_service import (
    DemoServiceAuthRequiredError,
    DemoSessionRequiredError,
)
from app.services.reservation.errors import (
    PublicReservationContractError,
    ReservationNotFoundError,
    ReservationReferenceRequestError,
)


def create_funnel_app(application_settings=None) -> FastAPI:
    current_settings = application_settings or get_application_settings()
    application = FastAPI(
        title=current_settings.APP_NAME,
        version=current_settings.VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.router.redirect_slashes = False

    # Starlette applies the last-added middleware first: size/framing is bounded
    # before a request can occupy one of the finite application work slots.
    application.add_middleware(PublicDemoConcurrencyMiddleware)
    application.add_middleware(RequestBodyLimitMiddleware)
    application.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )
    application.add_exception_handler(
        ConversationBusyError, conversation_busy_exception_handler
    )
    for error_type in (
        PostCommitMemoryPublicationError,
        ReservationMutationGuardError,
        ConversationMemoryValidationError,
        ConversationMemoryError,
        PersistenceOperationError,
        PersistenceOutcomeUnknownError,
        TransactionSessionUnusableError,
    ):
        application.add_exception_handler(error_type, transaction_exception_handler)
    for error_type in (DemoServiceAuthRequiredError, DemoSessionRequiredError):
        application.add_exception_handler(error_type, demo_session_exception_handler)
    for error_type in (
        DemoHistoryResetRequiredError,
        DemoChatRequestConflictError,
        DemoChatProviderError,
        DemoChatServiceUnavailableError,
        DemoChatProviderTimeoutError,
    ):
        application.add_exception_handler(error_type, demo_chat_exception_handler)
    application.add_exception_handler(
        DemoRateLimitExceededError, demo_rate_limit_exception_handler
    )
    for error_type in (
        ReservationReferenceRequestError,
        ReservationNotFoundError,
        PublicReservationContractError,
    ):
        application.add_exception_handler(
            error_type, public_reservation_exception_handler
        )

    application.include_router(demo_session_router)
    application.include_router(demo_chat_router)
    application.include_router(demo_reservation_reset_router)

    @application.get("/health", include_in_schema=False)
    async def health():
        return {"status": "healthy"}

    return application


app = create_funnel_app(get_application_settings())
