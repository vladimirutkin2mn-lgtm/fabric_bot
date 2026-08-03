"""Persist a fictional completed analysis using the real service and stub provider."""

import asyncio

from app.config import get_settings
from app.db.models import Analysis
from app.db.session import create_engine, create_session_factory
from app.providers.analytics import NoOpAnalyticsClient
from app.providers.llm.stub import StubLLMClient
from app.repositories.analyses import SqlAlchemyAnalysisRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.services.analysis_service import AnalysisService


async def main() -> None:
    settings = get_settings()
    engine = create_engine(str(settings.database_url))
    sessions = create_session_factory(engine)
    async with sessions() as session:
        user, _ = await SqlAlchemyUserRepository(session).get_or_create(
            900000001, None, "Demo", "ru"
        )
        analysis = Analysis(
            user_id=user.id,
            status="draft",
            intake_step="complete",
            normalized_conversation_json=[
                {
                    "id": "m1",
                    "speaker": "A",
                    "timestamp": None,
                    "text": "Привет! Как день?",
                    "source_order": 1,
                },
                {
                    "id": "m2",
                    "speaker": "B",
                    "timestamp": None,
                    "text": "Хорошо, спасибо!",
                    "source_order": 2,
                },
            ],
            participants_json={"A": "Demo A", "B": "Demo B"},
            user_participant_label="A",
            user_goal="Понять динамику общения",
            relationship_stage="new_connection",
            message_count=2,
            character_count=32,
        )
        session.add(analysis)
        await session.commit()
        result = await AnalysisService(
            SqlAlchemyAnalysisRepository(session),
            StubLLMClient(),
            NoOpAnalyticsClient(),
            "stub",
            "stub",
        ).analyze(analysis.id, user.id)
        print(f"analysis_id={analysis.id} status={result.status}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
