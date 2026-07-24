import asyncio
import json
import unittest

from app.middleware.request_body_limit import (
    MAX_REQUEST_BODY_BYTES,
    MAX_REQUEST_BODY_FRAMES,
    RequestBodyLimitMiddleware,
)


class AsgiHarness:
    def __init__(self):
        self.downstream_calls = 0
        self.received_body = None

    async def downstream(self, scope, receive, send):
        self.downstream_calls += 1
        event = await receive()
        self.received_body = event["body"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    def run(self, *, headers=(), chunks=(b"",)):
        events = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        ]
        receive_calls = 0
        sent = []

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            return events.pop(0)

        async def send(event):
            sent.append(event)

        middleware = RequestBodyLimitMiddleware(self.downstream)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/chat",
            "headers": list(headers),
        }
        asyncio.run(middleware(scope, receive, send))
        status = next(
            event["status"] for event in sent if event["type"] == "http.response.start"
        )
        body = b"".join(
            event.get("body", b"")
            for event in sent
            if event["type"] == "http.response.body"
        )
        return status, body, receive_calls


class RequestBodyLimitTests(unittest.TestCase):
    def setUp(self):
        self.harness = AsgiHarness()

    def test_exact_limit_is_accepted_and_replayed(self):
        body = b"x" * MAX_REQUEST_BODY_BYTES
        status, _response, _calls = self.harness.run(
            headers=[(b"content-length", str(len(body)).encode("ascii"))],
            chunks=(body[:8000], body[8000:]),
        )
        self.assertEqual(status, 204)
        self.assertEqual(self.harness.received_body, body)
        self.assertEqual(self.harness.downstream_calls, 1)

    def test_oversized_declared_length_is_rejected_before_receive(self):
        secret_header = b"16385"
        status, response, receive_calls = self.harness.run(
            headers=[(b"content-length", secret_header)],
            chunks=(b"never-read",),
        )
        self.assertEqual(status, 413)
        self.assertEqual(receive_calls, 0)
        self.assertEqual(self.harness.downstream_calls, 0)
        self.assertEqual(
            json.loads(response),
            {
                "code": "REQUEST_BODY_TOO_LARGE",
                "detail": "Request body is too large.",
            },
        )
        self.assertNotIn(secret_header, response)

    def test_missing_length_and_chunked_frames_are_bounded(self):
        status, _response, _calls = self.harness.run(
            chunks=(b"a" * 8000, b"b" * 8384),
        )
        self.assertEqual(status, 204)
        self.assertEqual(len(self.harness.received_body), MAX_REQUEST_BODY_BYTES)

        oversized = AsgiHarness()
        status, response, _calls = oversized.run(
            headers=[(b"transfer-encoding", b"chunked")],
            chunks=(b"a" * 8000, b"b" * 8385),
        )
        self.assertEqual(status, 413)
        self.assertEqual(oversized.downstream_calls, 0)
        self.assertEqual(json.loads(response)["code"], "REQUEST_BODY_TOO_LARGE")

    def test_malformed_negative_empty_and_conflicting_lengths_are_400(self):
        cases = (
            [(b"content-length", b"")],
            [(b"content-length", b"-1")],
            [(b"content-length", b"+1")],
            [(b"content-length", b"1.0")],
            [(b"content-length", b"abc")],
            [(b"content-length", b"1"), (b"content-length", b"2")],
            [(b"content-length", b"1,2")],
            [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
        )
        for headers in cases:
            with self.subTest(headers=len(headers)):
                harness = AsgiHarness()
                status, response, receive_calls = harness.run(
                    headers=headers,
                    chunks=(b"x",),
                )
                self.assertEqual(status, 400)
                self.assertEqual(receive_calls, 0)
                self.assertEqual(harness.downstream_calls, 0)
                self.assertEqual(
                    json.loads(response),
                    {
                        "code": "INVALID_REQUEST_FRAMING",
                        "detail": "Request framing is invalid.",
                    },
                )

    def test_excessively_long_content_length_is_safe_400(self):
        harness = AsgiHarness()
        status, response, receive_calls = harness.run(
            headers=[(b"content-length", b"9" * 10_000)],
            chunks=(b"",),
        )

        self.assertEqual(status, 400)
        self.assertEqual(receive_calls, 0)
        self.assertEqual(harness.downstream_calls, 0)
        self.assertEqual(json.loads(response)["code"], "INVALID_REQUEST_FRAMING")

    def test_identical_duplicate_or_comma_lengths_are_accepted(self):
        for headers in (
            [(b"content-length", b"3"), (b"content-length", b"3")],
            [(b"content-length", b"3,3")],
            [(b"content-length", b"03")],
        ):
            with self.subTest(headers=headers):
                harness = AsgiHarness()
                status, _response, _calls = harness.run(
                    headers=headers,
                    chunks=(b"abc",),
                )
                self.assertEqual(status, 204)
                self.assertEqual(harness.received_body, b"abc")

    def test_nonidentical_duplicate_representations_are_rejected(self):
        cases = (
            [(b"content-length", b"3"), (b"content-length", b"03")],
            [(b"content-length", b"3,003")],
            [(b"content-length", b"03,3")],
            [(b"content-length", b"3,")],
            [(b"content-length", b",3")],
            [(b"content-length", b"3,,3")],
            [(b"content-length", b"3, 3")],
        )
        for headers in cases:
            with self.subTest(headers=headers):
                harness = AsgiHarness()
                status, response, receive_calls = harness.run(
                    headers=headers,
                    chunks=(b"abc",),
                )
                self.assertEqual(status, 400)
                self.assertEqual(receive_calls, 0)
                self.assertEqual(harness.downstream_calls, 0)
                self.assertEqual(
                    json.loads(response),
                    {
                        "code": "INVALID_REQUEST_FRAMING",
                        "detail": "Request framing is invalid.",
                    },
                )

    def test_declared_actual_mismatch_is_safe_400(self):
        for declared, body in ((b"4", b"abc"), (b"2", b"abc")):
            with self.subTest(declared=declared):
                harness = AsgiHarness()
                status, response, _calls = harness.run(
                    headers=[(b"content-length", declared)],
                    chunks=(body,),
                )
                self.assertEqual(status, 400)
                self.assertEqual(harness.downstream_calls, 0)
                self.assertEqual(json.loads(response)["code"], "INVALID_REQUEST_FRAMING")

    def test_non_http_scope_passes_through(self):
        calls = []

        async def downstream(scope, receive, send):
            calls.append(scope["type"])

        middleware = RequestBodyLimitMiddleware(downstream)

        async def receive():
            return {"type": "websocket.disconnect"}

        async def send(_event):
            pass

        asyncio.run(
            middleware(
                {"type": "websocket", "headers": []},
                receive,
                send,
            )
        )
        self.assertEqual(calls, ["websocket"])

    def test_disconnect_before_body_completion_stops_without_response(self):
        downstream_calls = 0
        sent = []
        events = iter(
            (
                {"type": "http.request", "body": b"partial", "more_body": True},
                {"type": "http.disconnect"},
            )
        )

        async def downstream(_scope, _receive, _send):
            nonlocal downstream_calls
            downstream_calls += 1

        async def receive():
            return next(events)

        async def send(event):
            sent.append(event)

        asyncio.run(
            RequestBodyLimitMiddleware(downstream)(
                {"type": "http", "headers": []},
                receive,
                send,
            )
        )
        self.assertEqual(downstream_calls, 0)
        self.assertEqual(sent, [])

    def test_excessive_empty_frames_are_rejected_without_buffer_growth(self):
        downstream_calls = 0
        receive_calls = 0
        sent = []

        async def downstream(_scope, _receive, _send):
            nonlocal downstream_calls
            downstream_calls += 1

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            return {"type": "http.request", "body": b"", "more_body": True}

        async def send(event):
            sent.append(event)

        asyncio.run(
            RequestBodyLimitMiddleware(downstream)(
                {"type": "http", "headers": []},
                receive,
                send,
            )
        )
        self.assertEqual(receive_calls, MAX_REQUEST_BODY_FRAMES + 1)
        self.assertEqual(downstream_calls, 0)
        response = b"".join(
            event.get("body", b"")
            for event in sent
            if event["type"] == "http.response.body"
        )
        self.assertEqual(
            json.loads(response)["code"],
            "INVALID_REQUEST_FRAMING",
        )

    def test_worst_case_four_byte_utf8_boundary(self):
        emoji = "\U0001f600".encode("utf-8")
        exact_body = emoji * (MAX_REQUEST_BODY_BYTES // len(emoji))
        accepted = AsgiHarness()
        status, _response, _calls = accepted.run(chunks=(exact_body,))
        self.assertEqual(status, 204)
        self.assertEqual(accepted.received_body, exact_body)

        rejected = AsgiHarness()
        status, response, _calls = rejected.run(chunks=(exact_body, b"x"))
        self.assertEqual(status, 413)
        self.assertEqual(rejected.downstream_calls, 0)
        self.assertEqual(
            json.loads(response)["code"],
            "REQUEST_BODY_TOO_LARGE",
        )


if __name__ == "__main__":
    unittest.main()
