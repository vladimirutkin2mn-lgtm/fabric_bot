"""Optional manual OpenAI contract smoke test using fictional content only."""

import asyncio

from app.config import get_settings
from app.domain.analysis import AnalysisResult
from app.prompts.loader import load_prompts
from app.providers.llm.base import LLMRequest
from app.providers.llm.factory import create_llm_client


async def main() -> None:
    settings = get_settings()
    if settings.llm_provider != "openai":
        raise SystemExit("Set LLM_PROVIDER=openai and OPENAI_API_KEY for this manual command")
    prompts = load_prompts(settings.llm_prompt_version)
    request = LLMRequest(
        prompts.system,
        prompts.request.format(
            participant_labels="A,B",
            user_participant_label="A",
            user_goal="Понять взаимность диалога",
            relationship_stage="new_connection",
            messages_json=(
                '[{"id":"m1","speaker":"A","timestamp":null,"text":"Привет!",'
                '"source_order":1},{"id":"m2","speaker":"B","timestamp":null,'
                '"text":"Привет, как дела?","source_order":2}]'
            ),
        ),
        AnalysisResult.model_json_schema(),
        ("m1", "m2"),
        ("A", "B"),
    )
    completion = await create_llm_client(settings).generate_analysis(request)
    AnalysisResult.model_validate_json(completion.payload)
    print(f"provider={completion.provider} model={completion.model} schema_valid=true")


if __name__ == "__main__":
    asyncio.run(main())
