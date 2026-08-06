"""Source-independent concurrency bound for the public demo gateway."""

from __future__ import annotations

import asyncio

from fastapi.responses import JSONResponse


PUBLIC_DEMO_MAX_CONCURRENT_REQUESTS = 16


class PublicDemoConcurrencyMiddleware:
    def __init__(
        self,
        app,
        max_concurrent: int = PUBLIC_DEMO_MAX_CONCURRENT_REQUESTS,
    ):
        self.app = app
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=0.01)
        except TimeoutError:
            response = JSONResponse(
                status_code=503,
                content={
                    "code": "SERVICE_UNAVAILABLE",
                    "detail": "Layanan demo sementara tidak tersedia.",
                },
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            self._semaphore.release()
