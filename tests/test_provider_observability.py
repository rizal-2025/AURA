import asyncio
from contextlib import contextmanager
import io
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai

from app.agents.orchestrator import AgentOrchestrator
from app.core.locale import SupportedLocale, presentation_locale
from app.core.logger import logger
from app.services.ai.ollama_provider import OllamaProvider
from app.services.ai.openai_provider import OpenAIProvider
from app.services.conversation.general_conversation import (
    GeneralConversationService,
)
from app.services.demo_chat_service import DemoChatService


REQUEST_ID = "61d831fc-2708-4693-a008-3f09f906be7a"
DUMMY_KEY = "sk-test-provider-observability-not-real"
MODEL = "gpt-test-observability"


class _BrokenResponse:
    @property
    def output_text(self):
        raise ValueError("private extraction detail")


class ProviderObservabilityTests(unittest.TestCase):
    @staticmethod
    def _provider(*, result=None, error=None):
        config = SimpleNamespace(
            OPENAI_API_KEY=DUMMY_KEY,
            OPENAI_MODEL=MODEL,
            AI_PROVIDER_TIMEOUT_SECONDS=20,
        )
        with patch("app.services.ai.openai_provider.AsyncOpenAI"):
            provider = OpenAIProvider(config)
        create = AsyncMock(return_value=result, side_effect=error)
        provider.client = SimpleNamespace(
            responses=SimpleNamespace(create=create)
        )
        return provider, create

    @staticmethod
    @contextmanager
    def _captured_events():
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            yield stream
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

    @staticmethod
    def _events(stream):
        events = []
        for line in stream.getvalue().splitlines():
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("event"):
                events.append(candidate)
        return events

    @staticmethod
    def _request_response(status_code):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        return request, httpx.Response(status_code, request=request)

    def test_demo_canonical_request_id_reaches_request_scoped_orchestrator(self):
        service = DemoChatService()
        with patch(
            "app.services.demo_chat_service.AgentOrchestrator"
        ) as orchestrator_class:
            agent = orchestrator_class.return_value
            agent.memory_manager = MagicMock()
            service._build_core(1, request_id=REQUEST_ID)

        orchestrator_class.assert_called_once_with(
            provider_request_id=REQUEST_ID
        )

    def test_general_conversation_propagates_request_id_to_provider(self):
        provider = type(
            "Provider",
            (),
            {"chat": AsyncMock(return_value="AURA is ready to help.")},
        )()
        service = GeneralConversationService(provider)

        with presentation_locale(SupportedLocale.EN_US):
            reply = asyncio.run(
                service.respond("Hello", request_id=REQUEST_ID)
            )

        self.assertEqual(reply, "AURA is ready to help.")
        self.assertEqual(
            provider.chat.await_args.kwargs["request_id"],
            REQUEST_ID,
        )

    def test_success_emits_one_attempt_and_one_terminal_outcome(self):
        provider, create = self._provider(
            result=SimpleNamespace(output_text="Safe response body")
        )
        with self._captured_events() as captured:
            result = asyncio.run(
                provider.chat(
                    "private raw prompt",
                    instructions="private instructions",
                    request_id=REQUEST_ID,
                )
            )

        self.assertEqual(result, "Safe response body")
        create.assert_awaited_once()
        events = self._events(captured)
        self.assertEqual(
            [event["event"] for event in events],
            ["AI_PROVIDER_ATTEMPT", "AI_PROVIDER_OUTCOME"],
        )
        self.assertEqual(events[1]["outcome"], "SUCCESS")
        self.assertEqual({event["request_id"] for event in events}, {REQUEST_ID})
        self.assertEqual({event["provider"] for event in events}, {"openai"})
        self.assertEqual({event["model"] for event in events}, {MODEL})
        self.assertEqual(
            {event["operation"] for event in events},
            {"responses.create"},
        )
        log_text = captured.getvalue()
        for forbidden in (
            DUMMY_KEY,
            "Authorization",
            "private raw prompt",
            "private instructions",
            "Safe response body",
        ):
            self.assertNotIn(forbidden, log_text)

    def test_typed_failures_emit_exactly_one_bounded_terminal_outcome(self):
        request, auth_response = self._request_response(401)
        _, rate_response = self._request_response(429)
        _, billing_response = self._request_response(429)
        _, server_response = self._request_response(500)
        cases = (
            (
                "timeout",
                openai.APITimeoutError(request=request),
                "TIMEOUT",
            ),
            (
                "auth",
                openai.AuthenticationError(
                    "safe",
                    response=auth_response,
                    body={"code": "invalid_api_key"},
                ),
                "AUTH",
            ),
            (
                "rate_limit",
                openai.RateLimitError(
                    "safe",
                    response=rate_response,
                    body={"code": "rate_limit_exceeded"},
                ),
                "RATE_LIMIT",
            ),
            (
                "billing",
                openai.RateLimitError(
                    "safe",
                    response=billing_response,
                    body={"code": "insufficient_quota"},
                ),
                "BILLING",
            ),
            (
                "provider",
                openai.InternalServerError(
                    "safe",
                    response=server_response,
                    body={"code": "server_error"},
                ),
                "PROVIDER_ERROR",
            ),
        )
        for name, error, expected in cases:
            with self.subTest(name=name):
                provider, create = self._provider(error=error)
                with self._captured_events() as captured:
                    with self.assertRaises(type(error)):
                        asyncio.run(
                            provider.chat("private", request_id=REQUEST_ID)
                        )
                create.assert_awaited_once()
                events = self._events(captured)
                self.assertEqual(
                    [event["event"] for event in events],
                    ["AI_PROVIDER_ATTEMPT", "AI_PROVIDER_OUTCOME"],
                )
                self.assertEqual(events[1]["outcome"], expected)
                self.assertEqual(
                    sum(
                        event["event"] == "AI_PROVIDER_OUTCOME"
                        for event in events
                    ),
                    1,
                )

    def test_extraction_failure_is_one_client_error_terminal_outcome(self):
        provider, _ = self._provider(result=_BrokenResponse())
        with self._captured_events() as captured:
            with self.assertRaises(ValueError):
                asyncio.run(provider.chat("private", request_id=REQUEST_ID))

        events = self._events(captured)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["event"], "AI_PROVIDER_OUTCOME")
        self.assertEqual(events[1]["outcome"], "CLIENT_ERROR")

    def test_timeout_fallback_is_correlated_and_http200_text_is_unchanged(self):
        request, _ = self._request_response(408)
        provider, _ = self._provider(
            error=openai.APITimeoutError(request=request)
        )
        service = GeneralConversationService(provider)
        with presentation_locale(SupportedLocale.EN_US):
            with self._captured_events() as captured:
                reply = asyncio.run(
                    service.respond("private user text", request_id=REQUEST_ID)
                )

        self.assertEqual(
            reply,
            "Sorry, I can't answer general conversation right now. "
            "Please try again.",
        )
        events = self._events(captured)
        self.assertEqual(
            [event["event"] for event in events],
            [
                "AI_PROVIDER_ATTEMPT",
                "AI_PROVIDER_OUTCOME",
                "AI_PROVIDER_FALLBACK",
            ],
        )
        self.assertEqual(events[1]["outcome"], "TIMEOUT")
        self.assertEqual(events[2]["reason"], "TIMEOUT")
        self.assertEqual({event["request_id"] for event in events}, {REQUEST_ID})
        self.assertNotIn("private user text", captured.getvalue())

    def test_successful_general_response_emits_no_fallback(self):
        provider, _ = self._provider(
            result=SimpleNamespace(output_text="AURA can help with a demo.")
        )
        service = GeneralConversationService(provider)
        with presentation_locale(SupportedLocale.EN_US):
            with self._captured_events() as captured:
                reply = asyncio.run(
                    service.respond("Hello", request_id=REQUEST_ID)
                )

        self.assertEqual(reply, "AURA can help with a demo.")
        events = self._events(captured)
        self.assertNotIn(
            "AI_PROVIDER_FALLBACK",
            [event["event"] for event in events],
        )

    def test_request_scope_covers_classifier_and_general_provider_attempts(self):
        provider, create = self._provider()
        create.side_effect = (
            SimpleNamespace(
                output_text='{"intent":"general","confidence":0.99}'
            ),
            SimpleNamespace(output_text="AURA can help with a demo."),
        )
        orchestrator = AgentOrchestrator(
            ai_provider=provider,
            provider_request_id=REQUEST_ID,
        )
        with presentation_locale(SupportedLocale.EN_US):
            with self._captured_events() as captured:
                reply = asyncio.run(
                    orchestrator.handle(
                        "provider-observability-success",
                        "What do you do?",
                        MagicMock(),
                        "owner",
                    )
                )

        self.assertEqual(reply, "AURA can help with a demo.")
        self.assertEqual(create.await_count, 2)
        events = self._events(captured)
        self.assertEqual(
            [event["event"] for event in events],
            [
                "AI_PROVIDER_ATTEMPT",
                "AI_PROVIDER_OUTCOME",
                "AI_PROVIDER_ATTEMPT",
                "AI_PROVIDER_OUTCOME",
            ],
        )
        self.assertEqual({event["request_id"] for event in events}, {REQUEST_ID})
        self.assertEqual(
            [event["outcome"] for event in events if "outcome" in event],
            ["SUCCESS", "SUCCESS"],
        )

    def test_deterministic_greeting_emits_no_provider_attempt(self):
        provider, create = self._provider()
        orchestrator = AgentOrchestrator(
            ai_provider=provider,
            provider_request_id=REQUEST_ID,
        )
        with presentation_locale(SupportedLocale.EN_US):
            with self._captured_events() as captured:
                reply = asyncio.run(
                    orchestrator.handle(
                        "provider-observability-greeting",
                        "Hello",
                        MagicMock(),
                        "owner",
                    )
                )

        self.assertIn("Hello", reply)
        create.assert_not_awaited()
        self.assertEqual(self._events(captured), [])

    def test_classifier_failure_fallback_uses_same_request_id(self):
        request, _ = self._request_response(408)
        provider, _ = self._provider(
            error=openai.APITimeoutError(request=request)
        )
        orchestrator = AgentOrchestrator(
            ai_provider=provider,
            provider_request_id=REQUEST_ID,
        )
        with presentation_locale(SupportedLocale.EN_US):
            with self._captured_events() as captured:
                reply = asyncio.run(
                    orchestrator.handle(
                        "provider-observability",
                        "What do you do?",
                        MagicMock(),
                        "owner",
                    )
                )

        self.assertEqual(
            reply,
            "Sorry, I can't answer general conversation right now. "
            "Please try again.",
        )
        events = self._events(captured)
        self.assertEqual(
            [event["event"] for event in events],
            [
                "AI_PROVIDER_ATTEMPT",
                "AI_PROVIDER_OUTCOME",
                "AI_PROVIDER_FALLBACK",
            ],
        )
        self.assertEqual({event["request_id"] for event in events}, {REQUEST_ID})

    def test_ollama_accepts_request_id_without_changing_request_contract(self):
        config = SimpleNamespace(
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="qwen2.5:3b",
            AI_PROVIDER_TIMEOUT_SECONDS=20,
        )
        with patch("app.services.ai.ollama_provider.AsyncOpenAI"):
            provider = OllamaProvider(config)
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )
        )
        provider.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )

        result = asyncio.run(
            provider.chat("hello", request_id=REQUEST_ID)
        )

        self.assertEqual(result, "ok")
        create.assert_awaited_once_with(
            model="qwen2.5:3b",
            messages=[{"role": "user", "content": "hello"}],
        )


if __name__ == "__main__":
    unittest.main()
