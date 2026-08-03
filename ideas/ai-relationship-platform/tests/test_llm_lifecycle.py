"""Provider-neutral LLM lifecycle tests."""

from app.providers.llm.base import LLMCompletion, LLMRequest, close_llm_client
from app.providers.llm.stub import StubLLMClient


class PlainClient:
    async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
        raise AssertionError


class ClosingClient(PlainClient):
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_stub_and_plain_clients_close_safely() -> None:
    await close_llm_client(StubLLMClient())
    await close_llm_client(PlainClient())


async def test_closable_client_is_closed() -> None:
    client = ClosingClient()
    await close_llm_client(client)
    assert client.closed
