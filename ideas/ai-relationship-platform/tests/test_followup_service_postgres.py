"""Real-PostgreSQL concurrency and release invariants for paid follow-ups."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.followups import FollowUpQuestion
from app.db.models import Analysis, CreditTransaction, User
from app.providers.llm.base import LLMCompletion, LLMRequest, LLMTimeoutError
from app.providers.llm.stub import StubLLMClient
from app.services.followup_service import FollowUpService, FollowUpStatus
from app.services.sensitive_content import AESGCMSensitiveContentCipher, ContentPurpose

pytestmark = pytest.mark.postgres


class AnalyticsRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str | None, str, Mapping[str, str] | None]] = []

    async def track(
        self,
        user_id: str | None,
        event: str,
        properties: Mapping[str, str] | None = None,
    ) -> None:
        self.events.append((user_id, event, properties))


class SlowLLM:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def payload() -> str:
        return json.dumps(
            {
                "answer": "Смешанные сигналы лучше уточнить спокойным прямым вопросом.",
                "report_refs": ["summary", "next_actions.0"],
                "limitations": ["Ответ основан только на структурированном отчёте."],
                "safety": {"high_risk_detected": False, "categories": []},
            },
            ensure_ascii=False,
        )

    async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
        self.calls += 1
        await asyncio.sleep(0.2)
        return LLMCompletion(self.payload(), "fake", "fake-model", "request-1", 10, 20, 30)


class SequenceLLM:
    def __init__(self, *outputs: str | Exception) -> None:
        self.outputs = list(outputs)
        self.calls = 0
        self.requests: list[LLMRequest] = []

    async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
        self.calls += 1
        self.requests.append(request)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return LLMCompletion(output, "fake", "fake-model", f"request-{self.calls}", 1, 2, 3)


@pytest.fixture
async def followup_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def report_payload() -> dict[str, object]:
    request = LLMRequest("", "", {}, ("m1",), ("A", "B"))
    return StubLLMClient()._result(request, False)


async def paid_analysis(
    sessions: async_sessionmaker[AsyncSession], *, access: str = "full"
) -> tuple[UUID, UUID]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Follow-up")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="draft",
            intake_step="complete",
            source_type="text",
            message_count=1,
            character_count=10,
            report_access="none",
            cost_units=0,
        )
        session.add(analysis)
        await session.flush()
        spend = CreditTransaction(
            user_id=user.id,
            type="spend",
            amount=-1,
            idempotency_key=f"analysis_full_access:{analysis.id}",
            analysis_id=analysis.id,
        )
        session.add(spend)
        await session.flush()
        analysis.status = "completed"
        analysis.completed_at = datetime.now(UTC)
        analysis.result_json = report_payload()
        analysis.report_access = access
        analysis.cost_units = 1 if access == "full" else 0
        analysis.full_access_transaction_id = spend.id if access == "full" else None
        return user.id, analysis.id


def service(
    sessions: async_sessionmaker[AsyncSession], llm: SlowLLM | SequenceLLM
) -> FollowUpService:
    return FollowUpService(
        sessions,
        AESGCMSensitiveContentCipher("followup-test-key-material"),
        llm,
        AnalyticsRecorder(),
        "fake",
        "fake-model",
        lease_seconds=2,
    )


async def test_concurrent_requests_make_one_llm_call_and_consume_once(
    followup_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analysis_id = await paid_analysis(followup_db)
    llm = SlowLLM()
    followups = service(followup_db, llm)
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                followups.ask(analysis_id, user_id, "Что мне написать дальше?")
                for _ in range(10)
            )
        ),
        timeout=10,
    )

    assert llm.calls == 1
    assert {item.status for item in results} <= {
        FollowUpStatus.COMPLETED,
        FollowUpStatus.PROCESSING,
    }
    assert any(item.status is FollowUpStatus.COMPLETED for item in results)
    async with followup_db() as session:
        rows = int(await session.scalar(select(func.count()).select_from(FollowUpQuestion)) or 0)
        row = await session.scalar(
            select(FollowUpQuestion).where(FollowUpQuestion.analysis_id == analysis_id)
        )
        assert rows == 1
        assert row is not None and row.status == "completed"
        assert row.reservation_count == 1

    replay = await followups.ask(analysis_id, user_id, "Совсем другой второй вопрос")
    assert replay.status is FollowUpStatus.COMPLETED
    assert replay.idempotent
    assert replay.view is not None and replay.view.question == "Что мне написать дальше?"
    assert llm.calls == 1


async def test_technical_failure_releases_entitlement_for_retry(
    followup_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analysis_id = await paid_analysis(followup_db)
    llm = SequenceLLM(LLMTimeoutError(), SlowLLM.payload())
    followups = service(followup_db, llm)

    failed = await followups.ask(analysis_id, user_id, "Что делать?")
    assert failed.status is FollowUpStatus.FAILED_RELEASED
    assert (await followups.inspect(analysis_id, user_id)).status is FollowUpStatus.READY
    async with followup_db() as session:
        row = await session.scalar(
            select(FollowUpQuestion).where(FollowUpQuestion.analysis_id == analysis_id)
        )
        assert row is not None and row.status == "available"
        assert row.question_ciphertext is None and row.answer_ciphertext is None
        assert row.last_failure_code == "llm_timeout"

    completed = await followups.ask(analysis_id, user_id, "Что делать?")
    assert completed.status is FollowUpStatus.COMPLETED
    assert llm.calls == 2


async def test_invalid_first_answer_is_repaired_without_raw_conversation(
    followup_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analysis_id = await paid_analysis(followup_db)
    invalid = json.dumps(
        {
            "answer": "Ответ без допустимой ссылки.",
            "report_refs": ["observations.99"],
            "limitations": [],
            "safety": {"high_risk_detected": False, "categories": []},
        },
        ensure_ascii=False,
    )
    llm = SequenceLLM(invalid, SlowLLM.payload())
    completed = await service(followup_db, llm).ask(
        analysis_id, user_id, "Как спокойно уточнить ожидания?"
    )
    assert completed.status is FollowUpStatus.COMPLETED
    assert llm.calls == 2
    assert llm.requests[1].repair
    assert all(not request.message_ids for request in llm.requests)
    assert all(not request.participant_labels for request in llm.requests)
    assert all("Как спокойно уточнить ожидания?" in request.user_prompt for request in llm.requests)


async def test_expired_claim_is_reclaimed_and_stale_content_is_replaced(
    followup_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analysis_id = await paid_analysis(followup_db)
    cipher = AESGCMSensitiveContentCipher("followup-test-key-material")
    async with followup_db.begin() as session:
        session.add(
            FollowUpQuestion(
                analysis_id=analysis_id,
                user_id=user_id,
                status="reserved",
                claim_id=uuid4(),
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
                question_ciphertext=cipher.encrypt_json(
                    ContentPurpose.FOLLOW_UP_QUESTION,
                    {"question": "Старый вопрос"},
                ),
                prompt_version="followup_v1",
                reservation_count=1,
            )
        )
    llm = SequenceLLM(SlowLLM.payload())
    completed = await service(followup_db, llm).ask(analysis_id, user_id, "Новый вопрос")
    assert completed.status is FollowUpStatus.COMPLETED
    assert completed.view is not None and completed.view.question == "Новый вопрос"
    async with followup_db() as session:
        row = await session.scalar(
            select(FollowUpQuestion).where(FollowUpQuestion.analysis_id == analysis_id)
        )
        assert row is not None and row.reservation_count == 2


async def test_preview_and_deleted_analyses_cannot_use_followup(
    followup_db: async_sessionmaker[AsyncSession],
) -> None:
    preview_user, preview_analysis = await paid_analysis(followup_db, access="preview")
    llm = SequenceLLM(SlowLLM.payload())
    followups = service(followup_db, llm)
    assert (
        await followups.ask(preview_analysis, preview_user, "Вопрос")
    ).status is FollowUpStatus.NOT_ELIGIBLE

    user_id, analysis_id = await paid_analysis(followup_db)
    async with followup_db.begin() as session:
        analysis = await session.get(Analysis, analysis_id, with_for_update=True)
        assert analysis is not None
        analysis.status = "deleted"
        analysis.report_access = "none"
        analysis.completed_at = None
    assert (
        await followups.ask(analysis_id, user_id, "Вопрос")
    ).status is FollowUpStatus.NOT_ELIGIBLE
    assert llm.calls == 0


async def test_soft_delete_purges_encrypted_followup_history(
    followup_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analysis_id = await paid_analysis(followup_db)
    llm = SequenceLLM(SlowLLM.payload())
    followups = service(followup_db, llm)
    assert (
        await followups.ask(analysis_id, user_id, "Что дальше?")
    ).status is FollowUpStatus.COMPLETED
    async with followup_db.begin() as session:
        analysis = await session.get(Analysis, analysis_id, with_for_update=True)
        assert analysis is not None
        analysis.status = "deleted"
        analysis.report_access = "none"
        analysis.completed_at = None
    async with followup_db() as session:
        assert (
            int(await session.scalar(select(func.count()).select_from(FollowUpQuestion)) or 0) == 0
        )
