from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
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
from app.api.error_handlers import (
    conversation_busy_exception_handler,
    demo_session_exception_handler,
    request_validation_exception_handler,
    transaction_exception_handler,
)
from app.middleware.request_body_limit import RequestBodyLimitMiddleware
from app.services.demo_session_service import (
    DemoServiceAuthRequiredError,
    DemoSessionRequiredError,
)

application_settings = get_application_settings()

from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.reservation import router as reservation_router


def create_app(application_settings=None) -> FastAPI:
    current_settings = application_settings or get_application_settings()
    application = FastAPI(
        title=current_settings.APP_NAME,
        version=current_settings.VERSION,
    )
    application.add_middleware(RequestBodyLimitMiddleware)
    application.add_exception_handler(
        RequestValidationError,
        request_validation_exception_handler,
    )
    application.add_exception_handler(
        ConversationBusyError,
        conversation_busy_exception_handler,
    )
    for transaction_error in (
        PostCommitMemoryPublicationError,
        ReservationMutationGuardError,
        ConversationMemoryValidationError,
        ConversationMemoryError,
        PersistenceOperationError,
        PersistenceOutcomeUnknownError,
        TransactionSessionUnusableError,
    ):
        application.add_exception_handler(
            transaction_error,
            transaction_exception_handler,
        )
    for demo_session_error in (
        DemoServiceAuthRequiredError,
        DemoSessionRequiredError,
    ):
        application.add_exception_handler(
            demo_session_error,
            demo_session_exception_handler,
        )

    application.include_router(chat_router)
    application.include_router(auth_router)
    application.include_router(reservation_router)
    if current_settings.APP_ENV == "demo":
        from app.api.internal_demo_sessions import router as demo_session_router

        application.include_router(demo_session_router)

    @application.get("/")
    async def root():
        return {
            "application": current_settings.APP_NAME,
            "version": current_settings.VERSION,
            "status": "running",
        }

    @application.get("/health")
    async def health():
        return {"status": "healthy"}

    return application


app = create_app(application_settings)
