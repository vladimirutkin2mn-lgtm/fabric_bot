"""Milestone 3 persistence, constraints, and concurrency on real PostgreSQL."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base
from app.db.models import Analysis, User
from app.providers.analytics import NoOpAnalyticsClient
from app.providers.llm.base import LLMCompletion, LLMRequest, LLMTimeoutError
from app.providers.llm.stub import StubLLMClient
from app.repositories.analyses import ClaimOutcome, LLMMetadata, SqlAlchemyAnalysisRepository
from app.services.analysis_service import AnalysisService, AnalysisServiceStatus
from tests.test_analysis_service import valid_payload

pytestmark = pytest.mark.postgres


@pytest.fixture
async def postgres_m3() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def create_analysis(
    sessions: async_sessionmaker[AsyncSession], status: str = "draft", step: str = "complete"
) -> Analysis:
    async with sessions() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Fictional")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status=status,
            intake_step=step,
            normalized_conversation_json=[
                {
                    "id": "m1",
                    "speaker": "A",
                    "timestamp": None,
                    "text": "Привет",
                    "source_order": 1,
                },
                {
                    "id": "m2",
                    "speaker": "B",
                    "timestamp": None,
                    "text": "Привет",
                    "source_order": 2,
                },
            ],
            participants_json={"A": "Fictional A", "B": "Fictional B"},
            user_participant_label="A",
            user_goal="Понять общение",
            relationship_stage="dating",
            message_count=2,
            character_count=12,
            failure_code="safe_test_failure" if status == "failed" else None,
        )
        session.add(analysis)
        await session.commit()
        return analysis


async def test_milestone3_success_metadata_persists_across_sessions(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)
    metadata = LLMMetadata("stub", "stub", "analysis_v1", 1, 10, 20, 30, "req")
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        assert (
            await repository.claim_processing(analysis.id, analysis.user_id) == ClaimOutcome.CLAIMED
        )
        await repository.complete_processing(
            analysis.id, {"summary": "stored", "nested": None}, metadata
        )
    async with sessions() as session:
        stored = await session.get(Analysis, analysis.id)
        assert stored is not None and stored.status == "completed" and stored.result_json
        assert stored.result_json["nested"] is None
        assert stored.completed_at is not None and stored.failure_code is None
        assert (stored.llm_provider, stored.model_name, stored.prompt_version) == (
            "stub",
            "stub",
            "analysis_v1",
        )
        assert (stored.input_tokens, stored.output_tokens, stored.latency_ms) == (10, 20, 30)


@pytest.mark.parametrize(
    ("status", "step", "outcome"),
    [
        ("draft", "waiting_for_goal", ClaimOutcome.NOT_READY),
        ("deleted", "complete", ClaimOutcome.DELETED),
        ("processing", "complete", ClaimOutcome.PROCESSING),
        ("failed", "complete", ClaimOutcome.NOT_READY),
    ],
)
async def test_non_claimable_states(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    status: str,
    step: str,
    outcome: ClaimOutcome,
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions, status, step)
    async with sessions() as session:
        assert (
            await SqlAlchemyAnalysisRepository(session).claim_processing(
                analysis.id, analysis.user_id
            )
            == outcome
        )


async def test_completed_claim_is_idempotent(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        await repository.claim_processing(analysis.id, analysis.user_id)
        await repository.complete_processing(
            analysis.id, {"ok": True}, LLMMetadata("stub", "stub", "analysis_v1", 1)
        )
        assert (
            await repository.claim_processing(analysis.id, analysis.user_id)
            == ClaimOutcome.COMPLETED
        )


async def test_ten_concurrent_claims_have_exactly_one_winner(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)

    async def claim() -> ClaimOutcome:
        async with sessions() as session:
            return await SqlAlchemyAnalysisRepository(session).claim_processing(
                analysis.id, analysis.user_id
            )

    outcomes = await asyncio.gather(*(claim() for _ in range(10)))
    assert outcomes.count(ClaimOutcome.CLAIMED) == 1
    assert outcomes.count(ClaimOutcome.PROCESSING) == 9


class CountingLLM:
    def __init__(
        self, entered: asyncio.Event | None = None, release: asyncio.Event | None = None
    ) -> None:
        self.calls, self.entered, self.release = 0, entered, release

    async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
        self.calls += 1
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        else:
            await asyncio.sleep(0.1)
        return LLMCompletion(
            valid_payload(), "stub", "stub", input_tokens=1, output_tokens=2, latency_ms=3
        )


async def test_ten_concurrent_services_make_one_provider_call(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)
    llm = CountingLLM()

    async def analyze() -> AnalysisServiceStatus:
        async with sessions() as session:
            result = await AnalysisService(
                SqlAlchemyAnalysisRepository(session), llm, NoOpAnalyticsClient(), "stub", "stub"
            ).analyze(analysis.id, analysis.user_id)
            return result.status

    statuses = await asyncio.gather(*(analyze() for _ in range(10)))
    assert llm.calls == 1 and statuses.count(AnalysisServiceStatus.COMPLETED) == 1
    assert statuses.count(AnalysisServiceStatus.ALREADY_PROCESSING) == 9


async def test_claim_is_committed_before_provider_wait(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    llm = CountingLLM(entered, release)
    async with sessions() as service_session:
        task = asyncio.create_task(
            AnalysisService(
                SqlAlchemyAnalysisRepository(service_session),
                llm,
                NoOpAnalyticsClient(),
                "stub",
                "stub",
            ).analyze(analysis.id, analysis.user_id)
        )
        await asyncio.wait_for(entered.wait(), 2)
        async with sessions() as observer:
            assert (
                await observer.scalar(select(Analysis.status).where(Analysis.id == analysis.id))
                == "processing"
            )
            assert await observer.scalar(text("SELECT 1")) == 1
        release.set()
        assert (await task).status == AnalysisServiceStatus.COMPLETED


async def test_failure_persists_without_result_or_completed_timestamp(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        await repository.claim_processing(analysis.id, analysis.user_id)
        await repository.fail_processing(
            analysis.id, "llm_timeout", LLMMetadata("stub", "stub", "analysis_v1", 1)
        )
    async with sessions() as session:
        stored = await session.get(Analysis, analysis.id)
        assert stored is not None and stored.status == "failed"
        assert (
            stored.result_json is None
            and stored.completed_at is None
            and stored.failure_code == "llm_timeout"
        )
        sql_null = await session.scalar(
            select(Analysis.result_json.is_(None)).where(Analysis.id == analysis.id)
        )
        assert sql_null is True


async def test_real_service_timeout_persists_sql_null_result(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)

    class TimeoutLLM:
        async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
            raise LLMTimeoutError

    async with sessions() as session:
        result = await AnalysisService(
            SqlAlchemyAnalysisRepository(session),
            TimeoutLLM(),
            NoOpAnalyticsClient(),
            "stub",
            "stub",
        ).analyze(analysis.id, analysis.user_id)
        assert result.status == AnalysisServiceStatus.FAILED
        assert result.failure_code == "llm_timeout"
    async with sessions() as session:
        stored = await session.get(Analysis, analysis.id)
        assert stored is not None and stored.status == "failed"
        assert stored.result_json is None and stored.completed_at is None
        assert stored.failure_code == "llm_timeout"
        assert (
            await session.scalar(
                select(Analysis.result_json.is_(None)).where(Analysis.id == analysis.id)
            )
            is True
        )


async def test_real_service_stub_repository_demo_path(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)
    async with sessions() as session:
        result = await AnalysisService(
            SqlAlchemyAnalysisRepository(session),
            StubLLMClient(),
            NoOpAnalyticsClient(),
            "stub",
            "stub",
        ).analyze(analysis.id, analysis.user_id)
        assert result.status == AnalysisServiceStatus.COMPLETED
    async with sessions() as session:
        stored = await session.get(Analysis, analysis.id)
        assert stored is not None and stored.result_json is not None
        assert (stored.llm_provider, stored.model_name, stored.prompt_version) == (
            "stub",
            "stub",
            "analysis_v1",
        )


@pytest.mark.parametrize(
    "values",
    [
        {"llm_attempt_count": -1},
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"latency_ms": -1},
        {"status": "completed", "result_json": None, "completed_at": datetime.now(UTC)},
        {"status": "failed", "result_json": {"partial": True}, "failure_code": "safe"},
        {"status": "failed", "result_json": JSONB.NULL, "failure_code": "safe"},
        {
            "status": "failed",
            "result_json": None,
            "failure_code": "safe",
            "completed_at": datetime.now(UTC),
        },
    ],
)
async def test_database_rejects_invalid_terminal_or_negative_metadata(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], values: dict[str, object]
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)
    async with sessions() as session:
        stored = await session.get(Analysis, analysis.id)
        assert stored is not None
        for key, value in values.items():
            setattr(stored, key, value)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_ownership_is_enforced(
    postgres_m3: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres_m3
    analysis = await create_analysis(sessions)
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        assert await repository.get_owned(analysis.id, uuid4()) is None
        assert await repository.claim_processing(analysis.id, uuid4()) == ClaimOutcome.NOT_FOUND
