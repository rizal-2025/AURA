from fastapi import FastAPI
from app.core.config import settings
from app.api.chat import router as chat_router
from app.api.auth import router as auth_router

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION
)

app.include_router(chat_router)
app.include_router(auth_router)
from app.api.reservation import router as reservation_router
app.include_router(reservation_router)

@app.get("/")
async def root():
    return {
        "application": settings.APP_NAME,
        "version": settings.VERSION,
        "status": "running"
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy"
    }
