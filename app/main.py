from fastapi import FastAPI
from app.core.config import settings

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION
)

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