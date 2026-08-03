"""Validated provider construction."""

from app.config import Settings
from app.providers.llm.base import LLMClient
from app.providers.llm.openai import OpenAILLMClient
from app.providers.llm.stub import StubLLMClient


def create_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "stub":
        return StubLLMClient(settings.llm_model)
    if settings.llm_provider == "openai":
        key = settings.openai_api_key.get_secret_value().strip()
        if not key:
            raise ValueError("OPENAI_API_KEY is required for the openai provider")
        return OpenAILLMClient(
            key,
            settings.llm_model,
            settings.llm_timeout_seconds,
            settings.llm_max_transport_attempts,
        )
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider!r}")
