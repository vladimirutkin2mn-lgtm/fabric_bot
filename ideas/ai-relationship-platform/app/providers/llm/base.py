"""Clean LLM boundary with no vendor types."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    user_prompt: str
    schema: dict[str, object]
    message_ids: tuple[str, ...]
    participant_labels: tuple[str, ...]
    repair: bool = False


@dataclass(frozen=True)
class LLMCompletion:
    payload: str
    provider: str
    model: str
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


class LLMClient(Protocol):
    async def generate_analysis(self, request: LLMRequest) -> LLMCompletion: ...


@runtime_checkable
class ClosableLLMClient(Protocol):
    async def aclose(self) -> None: ...


async def close_llm_client(client: LLMClient) -> None:
    """Close lifecycle-aware providers; ordinary protocol implementations need no close."""
    if isinstance(client, ClosableLLMClient):
        await client.aclose()


class LLMError(Exception):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMAuthenticationError(LLMError):
    pass


class LLMInvalidRequestError(LLMError):
    pass


class LLMTransientError(LLMError):
    pass


class LLMUnexpectedError(LLMError):
    pass
