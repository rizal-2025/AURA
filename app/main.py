from fastapi import FastAPI
from app.core.config import get_application_settings

application_settings = get_application_settings()

app = FastAPI(
    title=application_settings.APP_NAME,
    version=application_settings.VERSION
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
