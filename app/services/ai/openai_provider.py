import json
import re
from time import perf_counter
from uuid import UUID

import openai
from openai import AsyncOpenAI

from app.core.config import get_ai_settings
from app.core.logger import logger
from app.services.ai.base import AIProvider


class OpenAIProvider(AIProvider):

    provider_name = "openai"
    operation_name = "responses.create"
    _BILLING_CODES = frozenset(
        {
            "billing_hard_limit_reached",
            "billing_not_active",
            "insufficient_quota",
        }
    )
    _SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

    def __init__(self, config=None):
        self.config = config or get_ai_settings()
        self.client = AsyncOpenAI(
            api_key=self.config.OPENAI_API_KEY,
            timeout=getattr(self.config, "AI_PROVIDER_TIMEOUT_SECONDS", 20),
            max_retries=0,
        )

    @staticmethod
    def _safe_request_id(request_id: str | None) -> str:
        if request_id is None:
            return "UNSCOPED"
        try:
            return str(UUID(str(request_id)))
        except (AttributeError, TypeError, ValueError):
            return "INVALID"

    @classmethod
    def _safe_model(cls, model) -> str:
        candidate = str(model)
        return candidate if cls._SAFE_MODEL.fullmatch(candidate) else "INVALID"

    @classmethod
    def _structured_error_code(cls, error: BaseException) -> str | None:
        body = getattr(error, "body", None)
        if not isinstance(body, dict):
            return None
        code = body.get("code")
        nested = body.get("error")
        if code is None and isinstance(nested, dict):
            code = nested.get("code")
        return code if isinstance(code, str) else None

    @classmethod
    def categorize_error(cls, error: BaseException) -> str:
        status_code = getattr(error, "status_code", None)
        structured_code = cls._structured_error_code(error)
        if isinstance(error, (openai.APITimeoutError, TimeoutError)):
            return "TIMEOUT"
        if isinstance(error, openai.AuthenticationError) or status_code in {
            401,
            403,
        }:
            return "AUTH"
        if status_code == 402 or structured_code in cls._BILLING_CODES:
            return "BILLING"
        if isinstance(error, openai.RateLimitError) or status_code == 429:
            return "RATE_LIMIT"
        if isinstance(error, openai.APIStatusError):
            if isinstance(status_code, int) and status_code >= 500:
                return "PROVIDER_ERROR"
            return "CLIENT_ERROR"
        if isinstance(error, (openai.APIConnectionError, openai.APIError)):
            return "PROVIDER_ERROR"
        if isinstance(error, (AttributeError, TypeError, ValueError)):
            return "CLIENT_ERROR"
        return "UNKNOWN_ERROR"

    @staticmethod
    def _emit_event(fields: dict[str, object], *, failure: bool = False) -> None:
        try:
            serialized = json.dumps(
                fields,
                separators=(",", ":"),
                sort_keys=True,
            )
            if failure:
                logger.warning(serialized)
            else:
                logger.info(serialized)
        except Exception:
            # Observability must not alter provider or fallback behavior. The
            # rollout consumer fails closed when expected evidence is absent.
            return

    def _base_event(self, request_id: str | None) -> dict[str, object]:
        return {
            "model": self._safe_model(self.config.OPENAI_MODEL),
            "operation": self.operation_name,
            "provider": self.provider_name,
            "request_id": self._safe_request_id(request_id),
        }

    def emit_fallback(
        self,
        *,
        request_id: str | None,
        reason: str,
        locale: str,
    ) -> None:
        fields = self._base_event(request_id)
        fields.update(
            {
                "event": "AI_PROVIDER_FALLBACK",
                "locale": locale if locale in {"en-US", "id-ID"} else "INVALID",
                "reason": (
                    reason
                    if reason
                    in {
                        "AUTH",
                        "BILLING",
                        "CLIENT_ERROR",
                        "PROVIDER_ERROR",
                        "RATE_LIMIT",
                        "TIMEOUT",
                        "UNKNOWN_ERROR",
                    }
                    else "UNKNOWN_ERROR"
                ),
            }
        )
        self._emit_event(fields, failure=True)

    async def chat(
        self,
        message: str,
        *,
        instructions: str | None = None,
        max_output_tokens: int | None = None,
        request_id: str | None = None,
    ) -> str:
        request = {
            "model": self.config.OPENAI_MODEL,
            "input": message,
        }
        if instructions is not None:
            request["instructions"] = instructions
        if max_output_tokens is not None:
            request["max_output_tokens"] = max_output_tokens

        base_event = self._base_event(request_id)
        self._emit_event({"event": "AI_PROVIDER_ATTEMPT", **base_event})
        started = perf_counter()
        try:
            response = await self.client.responses.create(**request)
            output_text = response.output_text
        except BaseException as error:
            self._emit_event(
                {
                    "elapsed_ms": max(0, int((perf_counter() - started) * 1000)),
                    "event": "AI_PROVIDER_OUTCOME",
                    "outcome": self.categorize_error(error),
                    **base_event,
                },
                failure=True,
            )
            raise
        self._emit_event(
            {
                "elapsed_ms": max(0, int((perf_counter() - started) * 1000)),
                "event": "AI_PROVIDER_OUTCOME",
                "outcome": "SUCCESS",
                **base_event,
            }
        )
        return output_text
