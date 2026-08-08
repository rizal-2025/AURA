"""Bounded fixed-field diagnostics for the public Funnel session boundary."""

from __future__ import annotations

import asyncio
import json
import time

from app.core.logger import logger


SESSION_CREATE_PATH = "/internal/demo/sessions"
SESSION_CREATE_ROUTE = "session_create"
MAX_OBSERVED_SESSION_CREATE_REQUESTS = 8


def _response_code(status: int | None) -> str:
    if status is None:
        return "NO_RESPONSE"
    if 200 <= status <= 299:
        return "RESPONSE_2XX"
    if 400 <= status <= 499:
        return "RESPONSE_4XX"
    if 500 <= status <= 599:
        return "RESPONSE_5XX"
    return "RESPONSE_OTHER"


def _emit(stage: str, elapsed_ms: int, code: str) -> None:
    """Emit only the four fixed diagnostic fields, never request values."""
    try:
        logger.info(
            json.dumps(
                {
                    "route": SESSION_CREATE_ROUTE,
                    "stage": stage,
                    "elapsedMs": max(0, elapsed_ms),
                    "code": code,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except Exception:
        # Diagnostics must never alter request handling.
        return


class FunnelIngressObservabilityMiddleware:
    """Observe a bounded number of exact session-create requests.

    The middleware deliberately ignores headers, query strings, client
    addresses, bodies, exception values, and every non-session route.
    """

    def __init__(
        self,
        app,
        max_observed_requests: int = MAX_OBSERVED_SESSION_CREATE_REQUESTS,
    ):
        if (
            isinstance(max_observed_requests, bool)
            or not isinstance(max_observed_requests, int)
            or max_observed_requests < 0
        ):
            raise ValueError("Observed request limit must be non-negative.")
        self.app = app
        self._remaining = max_observed_requests
        self._claim_lock = asyncio.Lock()

    async def _claim(self) -> bool:
        async with self._claim_lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True

    @staticmethod
    def _matches(scope) -> bool:
        return (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == SESSION_CREATE_PATH
        )

    async def __call__(self, scope, receive, send):
        if not self._matches(scope) or not await self._claim():
            await self.app(scope, receive, send)
            return

        started_at = time.monotonic_ns()
        _emit("arrival", 0, "REQUEST_RECEIVED")
        response_status: int | None = None
        response_complete = False
        disconnected = False
        send_failed = False
        terminal_code: str | None = None

        async def observed_receive():
            nonlocal disconnected
            event = await receive()
            if event.get("type") == "http.disconnect":
                disconnected = True
            return event

        async def observed_send(message):
            nonlocal response_status, response_complete, send_failed
            if message.get("type") == "http.response.start":
                raw_status = message.get("status")
                if isinstance(raw_status, int) and not isinstance(raw_status, bool):
                    response_status = raw_status
            try:
                await send(message)
            except BaseException:
                send_failed = True
                raise
            if (
                message.get("type") == "http.response.body"
                and not message.get("more_body", False)
            ):
                response_complete = True

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            terminal_code = (
                "TRANSPORT_SEND_FAILED" if send_failed else "REQUEST_CANCELLED"
            )
            raise
        except Exception:
            terminal_code = (
                "TRANSPORT_SEND_FAILED" if send_failed else "APPLICATION_EXCEPTION"
            )
            raise
        finally:
            if terminal_code is None:
                if send_failed:
                    terminal_code = "TRANSPORT_SEND_FAILED"
                elif response_complete:
                    terminal_code = _response_code(response_status)
                elif disconnected:
                    terminal_code = "CLIENT_DISCONNECTED"
                elif response_status is not None:
                    terminal_code = "RESPONSE_INCOMPLETE"
                else:
                    terminal_code = "NO_RESPONSE"
            elapsed_ms = (time.monotonic_ns() - started_at) // 1_000_000
            _emit("completion", elapsed_ms, terminal_code)
