"""Secret-safe fixed-field Funnel ingress observability contracts."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from app.middleware.funnel_ingress_observability import (
    FunnelIngressObservabilityMiddleware,
)


SESSION_SCOPE = {
    "type": "http",
    "method": "POST",
    "path": "/internal/demo/sessions",
    "query_string": b"",
    "headers": [],
}


async def empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def accepting_send(_message):
    return None


async def successful_app(_scope, _receive, send):
    await send({"type": "http.response.start", "status": 201, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


class FunnelIngressObservabilityTests(unittest.TestCase):
    @staticmethod
    def diagnostics(captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    def test_success_emits_only_two_exact_four_field_events(self):
        middleware = FunnelIngressObservabilityMiddleware(successful_app)
        hostile_scope = {
            **SESSION_SCOPE,
            "query_string": b"token=never-log-this-query",
            "headers": [
                (b"x-bff-service-token", b"never-log-this-service-token"),
                (b"x-demo-client-subject", b"never-log-this-subject"),
            ],
            "client": ("203.0.113.9", 45123),
            "server": ("private-host.invalid", 443),
        }

        with self.assertLogs("AURA", level="INFO") as captured:
            asyncio.run(
                middleware(hostile_scope, empty_receive, accepting_send)
            )

        diagnostics = self.diagnostics(captured)
        self.assertEqual(len(diagnostics), 2)
        for diagnostic in diagnostics:
            self.assertEqual(
                set(diagnostic), {"route", "stage", "elapsedMs", "code"}
            )
            self.assertEqual(diagnostic["route"], "session_create")
            self.assertIsInstance(diagnostic["elapsedMs"], int)
            self.assertGreaterEqual(diagnostic["elapsedMs"], 0)
        self.assertEqual(
            diagnostics[0],
            {
                "code": "REQUEST_RECEIVED",
                "elapsedMs": 0,
                "route": "session_create",
                "stage": "arrival",
            },
        )
        self.assertEqual(diagnostics[1]["stage"], "completion")
        self.assertEqual(diagnostics[1]["code"], "RESPONSE_2XX")
        rendered = "\n".join(record.getMessage() for record in captured.records)
        for forbidden in (
            "never-log-this-query",
            "never-log-this-service-token",
            "never-log-this-subject",
            "203.0.113.9",
            "private-host.invalid",
            "/internal/demo/sessions",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_non_session_routes_and_methods_are_not_observed(self):
        middleware = FunnelIngressObservabilityMiddleware(successful_app)
        for scope in (
            {**SESSION_SCOPE, "method": "GET"},
            {**SESSION_SCOPE, "path": "/health"},
            {**SESSION_SCOPE, "type": "websocket"},
        ):
            with self.subTest(scope=scope):
                with self.assertNoLogs("AURA", level="INFO"):
                    asyncio.run(
                        middleware(scope, empty_receive, accepting_send)
                    )

    def test_application_exception_uses_fixed_code_without_exception_text(self):
        marker = "never-log-application-exception"

        async def failing_app(_scope, _receive, _send):
            raise RuntimeError(marker)

        middleware = FunnelIngressObservabilityMiddleware(failing_app)
        with self.assertLogs("AURA", level="INFO") as captured:
            with self.assertRaisesRegex(RuntimeError, marker):
                asyncio.run(
                    middleware(SESSION_SCOPE, empty_receive, accepting_send)
                )
        diagnostics = self.diagnostics(captured)
        self.assertEqual(diagnostics[-1]["code"], "APPLICATION_EXCEPTION")
        self.assertNotIn(
            marker,
            "\n".join(record.getMessage() for record in captured.records),
        )

    def test_send_failure_uses_fixed_code_without_transport_error_text(self):
        marker = "never-log-transport-error"

        async def failing_send(message):
            if message.get("type") == "http.response.body":
                raise ConnectionResetError(marker)

        middleware = FunnelIngressObservabilityMiddleware(successful_app)
        with self.assertLogs("AURA", level="INFO") as captured:
            with self.assertRaisesRegex(ConnectionResetError, marker):
                asyncio.run(
                    middleware(SESSION_SCOPE, empty_receive, failing_send)
                )
        diagnostics = self.diagnostics(captured)
        self.assertEqual(diagnostics[-1]["code"], "TRANSPORT_SEND_FAILED")
        self.assertNotIn(
            marker,
            "\n".join(record.getMessage() for record in captured.records),
        )

    def test_disconnect_before_response_uses_fixed_code(self):
        async def disconnect_receive():
            return {"type": "http.disconnect"}

        async def receive_only_app(_scope, receive, _send):
            await receive()

        middleware = FunnelIngressObservabilityMiddleware(receive_only_app)
        with self.assertLogs("AURA", level="INFO") as captured:
            asyncio.run(
                middleware(SESSION_SCOPE, disconnect_receive, accepting_send)
            )
        diagnostics = self.diagnostics(captured)
        self.assertEqual(diagnostics[-1]["code"], "CLIENT_DISCONNECTED")

    def test_observation_volume_is_bounded_per_process(self):
        middleware = FunnelIngressObservabilityMiddleware(
            successful_app,
            max_observed_requests=2,
        )
        with self.assertLogs("AURA", level="INFO") as captured:
            for _attempt in range(3):
                asyncio.run(
                    middleware(SESSION_SCOPE, empty_receive, accepting_send)
                )
        self.assertEqual(len(captured.records), 4)

    def test_logging_failure_never_changes_request_result(self):
        sent = []

        async def recording_send(message):
            sent.append(message)

        middleware = FunnelIngressObservabilityMiddleware(successful_app)
        with patch(
            "app.middleware.funnel_ingress_observability.logger.info",
            side_effect=RuntimeError("diagnostic logger unavailable"),
        ):
            asyncio.run(
                middleware(SESSION_SCOPE, empty_receive, recording_send)
            )
        self.assertEqual(sent[0]["status"], 201)
        self.assertEqual(sent[-1]["type"], "http.response.body")

    def test_invalid_observation_limit_fails_closed(self):
        for invalid in (-1, True, "8"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    FunnelIngressObservabilityMiddleware(
                        successful_app,
                        max_observed_requests=invalid,
                    )


if __name__ == "__main__":
    unittest.main()
