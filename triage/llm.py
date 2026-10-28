"""Pooled asynchronous structured-output client for OpenAI-compatible endpoints."""

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from triage.errors import TriageError
from triage.models import Signal, TriageDecision
from triage.settings import Settings

logger = logging.getLogger(__name__)
SYSTEM_PROMPT = Path(__file__).with_name("system_prompt.txt").read_text(encoding="utf-8")


class Usage(BaseModel):
    model_config = ConfigDict(strict=True)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class Message(BaseModel):
    content: str | None = None
    refusal: str | None = None


class Choice(BaseModel):
    message: Message
    finish_reason: str


class Completion(BaseModel):
    choices: list[Choice] = Field(min_length=1, max_length=1)
    usage: Usage


@dataclass(frozen=True)
class ModelResult:
    decision: TriageDecision
    usage: Usage
    estimated_cost: Decimal | None
    cache_hit: bool = False


@dataclass(frozen=True)
class JsonCompletion:
    content: str
    usage: Usage
    estimated_cost: Decimal | None


class LLMClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def classify(self, signal: Signal, *, deadline: float) -> ModelResult:
        """Classify a signal using the shared bounded completion transport."""
        result = await self.complete_json(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": signal.model_dump_json()},
            ],
            deadline=deadline,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "triage_decision",
                    "strict": True,
                    "schema": TriageDecision.model_json_schema(),
                },
            },
        )
        try:
            decision = TriageDecision.model_validate_json(result.content)
        except ValidationError as exc:
            raise TriageError(
                "MODEL_INVALID_RESPONSE", "The model returned an invalid triage response.", 502
            ) from exc
        return ModelResult(decision, result.usage, result.estimated_cost)

    async def complete_json(
        self,
        *,
        messages: list[dict[str, object]],
        deadline: float,
        response_format: dict[str, object],
        model: str | None = None,
        attempt_seconds: float | None = None,
        max_tokens: int = 1800,
    ) -> JsonCompletion:
        """Request one JSON completion with shared retry, refusal and usage checks."""
        if not self.settings.configured:
            raise TriageError(
                "MODEL_NOT_CONFIGURED", "Configure TRIAGE_LLM_API_KEY before model requests.", 503
            )
        key = self.settings.llm_api_key.get_secret_value()
        headers = (
            {"Authorization": f"Bearer {key}"}
            if self.settings.llm_auth == "bearer"
            else {"api-key": key}
        )
        payload = {
            "model": model or self.settings.llm_model,
            "messages": messages,
            "response_format": response_format,
            "max_completion_tokens": max_tokens,
            "temperature": 0,
            "seed": 42,
        }
        response = await self._request(
            headers,
            payload,
            deadline=deadline,
            attempt_seconds=attempt_seconds or self.settings.llm_timeout_seconds,
        )
        try:
            completion = Completion.model_validate_json(response.content)
            choice = completion.choices[0]
            if choice.message.refusal:
                raise TriageError("MODEL_REFUSED", "The model declined the request.", 502)
            if choice.finish_reason != "stop" or not choice.message.content:
                raise TriageError(
                    "MODEL_INCOMPLETE_RESPONSE",
                    "The model did not return a complete JSON record.",
                    502,
                )
            content = choice.message.content
        except ValidationError as exc:
            raise TriageError(
                "MODEL_INVALID_RESPONSE", "The model returned an invalid completion envelope.", 502
            ) from exc
        usage = completion.usage
        if usage.total_tokens != usage.prompt_tokens + usage.completion_tokens:
            raise TriageError(
                "MODEL_INVALID_RESPONSE", "The model returned invalid token usage.", 502
            )
        cost = None
        if (
            (model is None or model == self.settings.llm_model)
            and self.settings.input_price_per_million is not None
            and self.settings.output_price_per_million is not None
        ):
            cost = (
                usage.prompt_tokens * self.settings.input_price_per_million
                + usage.completion_tokens * self.settings.output_price_per_million
            ) / Decimal(1_000_000)
        return JsonCompletion(content, usage, cost)

    async def _request(
        self,
        headers: dict[str, str],
        payload: dict[str, object],
        *,
        deadline: float,
        attempt_seconds: float,
    ) -> httpx.Response:
        for attempt in range(self.settings.llm_attempts):
            retry_delay = 0.25 * 2**attempt
            try:
                remaining = min(
                    attempt_seconds, max(0.001, deadline - asyncio.get_running_loop().time())
                )
                async with asyncio.timeout(remaining):
                    response = await self.client.post(
                        self.settings.llm_url,
                        headers=headers,
                        content=json.dumps(payload),
                        timeout=remaining,
                    )
            except (TimeoutError, httpx.TimeoutException) as exc:
                error = TriageError(
                    "MODEL_TIMEOUT", "The model request timed out; retry later.", 504
                )
                cause: Exception | None = exc
            except httpx.RequestError as exc:
                error = TriageError(
                    "MODEL_UNAVAILABLE", "The model could not be reached; retry later.", 503
                )
                cause = exc
            else:
                if response.status_code == 200:
                    return response
                if response.status_code != 429 and response.status_code < 500:
                    raise TriageError(
                        "MODEL_REQUEST_REJECTED", "The model provider rejected the request.", 502
                    )
                error = TriageError(
                    "MODEL_UNAVAILABLE",
                    "The model provider is temporarily unavailable.",
                    503,
                    retry_after=retry_after_seconds(response.headers.get("Retry-After")),
                )
                if error.retry_after is not None:
                    retry_delay = max(retry_delay, error.retry_after)
                cause = None
            remaining = deadline - asyncio.get_running_loop().time()
            if attempt + 1 == self.settings.llm_attempts or retry_delay + 0.1 >= remaining:
                raise error from cause
            logger.warning("model_retry", extra={"code": error.code, "attempt": attempt + 1})
            await asyncio.sleep(retry_delay)
        raise AssertionError("Settings require at least one model attempt")


def retry_after_seconds(value: str | None) -> int | None:
    """Interpret provider Retry-After seconds or HTTP dates without shortening the delay."""
    if value is None:
        return None
    try:
        numeric = float(value)
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return max(0, math.ceil((timestamp - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return math.ceil(numeric)
