"""Official OpenAI Responses API adapter."""

import time
from typing import Any

import openai
from openai import AsyncOpenAI

from app.providers.llm.base import (
    LLMAuthenticationError,
    LLMCompletion,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMTransientError,
    LLMUnexpectedError,
)


class OpenAILLMClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_attempts: int,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenAI API key is required")
        self._client = client or AsyncOpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=0
        )
        self._model, self._timeout, self._max_attempts = model, timeout_seconds, max_attempts

    async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
        started = time.monotonic()
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.responses.create(
                    model=self._model,
                    input=[
                        {"role": "system", "content": request.system_prompt},
                        {"role": "user", "content": request.user_prompt},
                    ],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "analysis_result",
                            "strict": True,
                            "schema": request.schema,
                        }
                    },
                    store=False,
                    timeout=self._timeout,
                )
                usage = getattr(response, "usage", None)
                return LLMCompletion(
                    payload=response.output_text,
                    provider="openai",
                    model=getattr(response, "model", None) or self._model,
                    provider_request_id=getattr(response, "_request_id", None),
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except openai.APITimeoutError as error:
                raise LLMTimeoutError from error
            except openai.RateLimitError as error:
                raise LLMRateLimitError from error
            except openai.AuthenticationError as error:
                raise LLMAuthenticationError from error
            except (
                openai.BadRequestError,
                openai.PermissionDeniedError,
                openai.NotFoundError,
            ) as error:
                raise LLMInvalidRequestError from error
            except (openai.APIConnectionError, openai.InternalServerError) as error:
                if attempt == self._max_attempts:
                    raise LLMTransientError from error
            except openai.OpenAIError as error:
                raise LLMUnexpectedError from error
            except Exception as error:
                raise LLMUnexpectedError from error
        raise LLMTransientError
