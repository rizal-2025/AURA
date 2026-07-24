"""Bounded pure-ASGI request-body enforcement."""

from __future__ import annotations

import re

from app.api.error_handlers import (
    invalid_request_framing_response,
    request_body_too_large_response,
)


MAX_REQUEST_BODY_BYTES = 16_384
MAX_REQUEST_BODY_FRAMES = 1_024
_ASCII_DECIMAL = re.compile(rb"^[0-9]+$")


class InvalidRequestFraming(ValueError):
    """Internal framing sentinel that never retains header values."""


def _content_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    raw_lengths = [
        value for name, value in headers if name.lower() == b"content-length"
    ]
    has_transfer_encoding = any(
        name.lower() == b"transfer-encoding" for name, _value in headers
    )
    if raw_lengths and has_transfer_encoding:
        raise InvalidRequestFraming
    if not raw_lengths:
        return None

    raw_tokens: list[bytes] = []
    for raw_value in raw_lengths:
        for token in raw_value.split(b","):
            if (
                not token
                or len(token) > 20
                or _ASCII_DECIMAL.fullmatch(token) is None
            ):
                raise InvalidRequestFraming
            raw_tokens.append(token)
    if not raw_tokens or len(set(raw_tokens)) != 1:
        raise InvalidRequestFraming
    return int(raw_tokens[0])


class RequestBodyLimitMiddleware:
    def __init__(self, app, max_body_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        try:
            declared_length = _content_length(scope.get("headers", []))
        except InvalidRequestFraming:
            await invalid_request_framing_response()(scope, receive, send)
            return

        if declared_length is not None and declared_length > self.max_body_bytes:
            await request_body_too_large_response()(scope, receive, send)
            return

        body = bytearray()
        frame_count = 0
        while True:
            event = await receive()
            if event.get("type") == "http.disconnect":
                return
            if event.get("type") != "http.request":
                await invalid_request_framing_response()(scope, receive, send)
                return
            frame_count += 1
            if frame_count > MAX_REQUEST_BODY_FRAMES:
                await invalid_request_framing_response()(scope, receive, send)
                return

            chunk = event.get("body", b"")
            if not isinstance(chunk, bytes):
                await invalid_request_framing_response()(scope, receive, send)
                return
            remaining = self.max_body_bytes - len(body)
            if len(chunk) > remaining:
                body.extend(chunk[: remaining + 1])
                await request_body_too_large_response()(scope, receive, send)
                return
            body.extend(chunk)
            if not event.get("more_body", False):
                break

        if declared_length is not None and declared_length != len(body):
            await invalid_request_framing_response()(scope, receive, send)
            return

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay_receive, send)
