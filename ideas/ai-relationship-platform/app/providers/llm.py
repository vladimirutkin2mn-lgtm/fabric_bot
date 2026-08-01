"""Provider-neutral placeholder for the future analysis pipeline."""

from typing import Protocol


class LLMClient(Protocol):
    """Minimal boundary extended with domain request/result types in Milestone 3."""

    async def healthcheck(self) -> bool:
        """Return whether the provider is available."""
        ...


class StubLLMClient:
    """Local placeholder that performs no content analysis."""

    async def healthcheck(self) -> bool:
        """Keep local bootstrap independent from an external LLM."""
        return True
