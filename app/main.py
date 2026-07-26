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
    request_validation_exception_handler,
    transaction_exception_handler,
)
from app.middleware.request_body_limit import RequestBodyLimitMiddleware

application_settings = get_application_settings()

app = FastAPI(
    title=application_settings.APP_NAME,
    version=application_settings.VERSION
)
app.add_middleware(RequestBodyLimitMiddleware)
app.add_exception_handler(
    RequestValidationError,
    request_validation_exception_handler,
)
app.add_exception_handler(
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
    app.add_exception_handler(
        transaction_error,
        transaction_exception_handler,
    )

from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.reservation import router as reservation_router

app.include_router(chat_router)
app.include_router(auth_router)
app.include_router(reservation_router)

@app.get("/")
async def root():
    return {
        "application": application_settings.APP_NAME,
        "version": application_settings.VERSION,
        "status": "running"
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy"
    }
