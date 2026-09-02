import asyncio
import json
from types import SimpleNamespace
import unittest

import httpx
import openai
from openai import AsyncOpenAI

from app.services.ai.openai_provider import OpenAIProvider
from app.services.conversation.general_conversation import GeneralConversationService


DUMMY_API_KEY = "sk-test-aura-sdk-contract-only-not-real"
EXPECTED_OPENAI_VERSION = "1.66.5"


def _response_payload(output):
    return {
        "id": "resp_aura_contract_test",
        "object": "response",
        "created_at": 1741680000,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": 300,
        "model": "gpt-4.1-mini",
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": True,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
        "user": None,
        "metadata": {},
    }


def _message(message_id, content):
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": content,
    }


class TestOpenAISDKContract(unittest.TestCase):
    def test_real_installed_sdk_exposes_responses_resource(self):
        # Do not replace AsyncOpenAI here: this test guards the dependency/runtime
        # contract that a provider mock cannot prove.
        self.assertEqual(openai.__version__, EXPECTED_OPENAI_VERSION)

        async def verify_resource():
            client = AsyncOpenAI(api_key=DUMMY_API_KEY)
            try:
                self.assertTrue(hasattr(client, "responses"))
                self.assertTrue(callable(client.responses.create))
            finally:
                await client.close()

        asyncio.run(verify_resource())

    def test_real_client_preserves_timeout_and_zero_retry_budget(self):
        async def verify_budget():
            client = AsyncOpenAI(
                api_key=DUMMY_API_KEY,
                timeout=20,
                max_retries=0,
            )
            try:
                self.assertEqual(client.timeout, 20)
                self.assertEqual(client.max_retries, 0)
            finally:
                await client.close()

        asyncio.run(verify_budget())

    @staticmethod
    async def _exercise_provider(
        payload,
        input_text="bounded AURA input",
        instructions="stable AURA instructions",
    ):
        captured = {}

        async def handler(request):
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, request=request, json=payload)

        provider = OpenAIProvider(
            SimpleNamespace(
                OPENAI_API_KEY=DUMMY_API_KEY,
                OPENAI_MODEL="gpt-4.1-mini",
                AI_PROVIDER_TIMEOUT_SECONDS=20,
            )
        )
        initial_client = provider.client
        provider.client = AsyncOpenAI(
            api_key=DUMMY_API_KEY,
            timeout=20,
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
            ),
        )
        try:
            result = await provider.chat(
                input_text,
                instructions=instructions,
                max_output_tokens=300,
            )
        finally:
            await provider.client.close()
            await initial_client.close()
        return result, captured

    def test_real_sdk_accepts_gpt41_mini_request_shape_without_tools(self):
        instructions = GeneralConversationService.build_instructions()
        input_text = GeneralConversationService.build_input(
            "What can AURA do?",
            [{"role": "user", "content": "Hello"}],
        )
        payload = _response_payload(
            [
                _message(
                    "msg_aura_contract_test",
                    [
                        {
                            "type": "output_text",
                            "text": "AURA response",
                            "annotations": [],
                        }
                    ],
                )
            ]
        )

        result, captured = asyncio.run(
            self._exercise_provider(
                payload,
                input_text=input_text,
                instructions=instructions,
            )
        )

        self.assertEqual(result, "AURA response")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(
            captured["body"],
            {
                "model": "gpt-4.1-mini",
                "instructions": instructions,
                "input": input_text,
                "max_output_tokens": 300,
            },
        )
        self.assertNotIn("tools", captured["body"])
        self.assertNotIn("tool_choice", captured["body"])
        self.assertNotIn("reasoning", captured["body"])
        self.assertIn("Produce text only", captured["body"]["instructions"])
        self.assertNotIn("Produce text only", captured["body"]["input"])
        self.assertIn(
            "untrusted conversation data",
            captured["body"]["input"].lower(),
        )
        self.assertNotIn(DUMMY_API_KEY, captured["body"]["input"])
        self.assertNotIn("postgresql://", captured["body"]["input"])
        self.assertNotIn("C:\\", captured["body"]["input"])

    def test_real_sdk_response_extraction_contract(self):
        cases = (
            (
                "empty output",
                [],
                "",
            ),
            (
                "multiple output text items",
                [
                    _message(
                        "msg_aura_multiple_1",
                        [
                            {
                                "type": "output_text",
                                "text": "First",
                                "annotations": [],
                            },
                            {
                                "type": "output_text",
                                "text": " second",
                                "annotations": [],
                            },
                        ],
                    ),
                    _message(
                        "msg_aura_multiple_2",
                        [
                            {
                                "type": "output_text",
                                "text": " third",
                                "annotations": [],
                            }
                        ],
                    ),
                ],
                "First second third",
            ),
            (
                "refusal only",
                [
                    _message(
                        "msg_aura_refusal",
                        [
                            {
                                "type": "refusal",
                                "refusal": "Cannot comply",
                            }
                        ],
                    )
                ],
                "",
            ),
        )

        for name, output, expected in cases:
            with self.subTest(name=name):
                payload = _response_payload(output)
                result, captured = asyncio.run(
                    self._exercise_provider(payload)
                )
                self.assertEqual(result, expected)
                self.assertEqual(
                    set(captured["body"]),
                    {
                        "model",
                        "instructions",
                        "input",
                        "max_output_tokens",
                    },
                )


if __name__ == "__main__":
    unittest.main()
