"""Conversation intake service transitions, idempotency, and privacy."""

from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest

from app.db.models import Analysis, User
from app.services.conversation_intake import ConversationIntakeService, InvalidTransition
from app.services.conversation_parser import ConversationParser, ConversationRejected


class MemoryAnalyses:
    def __init__(self) -> None:
        self.items: dict[UUID, Analysis] = {}

    async def create_or_resume(self, user_id: UUID) -> tuple[Analysis, bool]:
        active = await self.get_active(user_id)
        if active:
            return active, False
        item = Analysis(
            id=uuid4(),
            user_id=user_id,
            status="draft",
            intake_step="waiting_for_conversation",
            source_type="text",
            message_count=0,
            character_count=0,
        )
        self.items[item.id] = item
        return item, True

    async def get_active(self, user_id: UUID) -> Analysis | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.user_id == user_id
                and item.status == "draft"
                and item.intake_step != "complete"
            ),
            None,
        )

    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None:
        item = self.items.get(analysis_id)
        return item if item and item.user_id == user_id else None

    async def get_latest_pending_billing(self, user_id: UUID) -> Analysis | None:
        return next(
            (
                item
                for item in reversed(tuple(self.items.values()))
                if item.user_id == user_id
                and item.status == "draft"
                and item.intake_step == "complete"
            ),
            None,
        )

    async def save(self, analysis: Analysis) -> None:
        self.items[analysis.id] = analysis

    async def cancel(self, analysis: Analysis) -> None:
        analysis.status = "deleted"
        analysis.normalized_conversation_json = None
        analysis.participants_json = None
        analysis.user_participant_label = None
        analysis.user_goal = None
        analysis.relationship_stage = None
        analysis.message_count = 0
        analysis.character_count = 0
        await self.save(analysis)


class RecordingAnalytics:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, str] | None]] = []

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        self.events.append((event, properties))


@pytest.fixture
def service_parts() -> tuple[ConversationIntakeService, MemoryAnalyses, RecordingAnalytics, User]:
    repository, analytics = MemoryAnalyses(), RecordingAnalytics()
    user = User(id=uuid4(), telegram_user_id=1, first_name="Test")
    return (
        ConversationIntakeService(repository, ConversationParser(), analytics, goal_limit=20),
        repository,
        analytics,
        user,
    )


async def test_create_resume_transitions_reset_and_restart(
    service_parts: tuple[ConversationIntakeService, MemoryAnalyses, RecordingAnalytics, User],
) -> None:
    service, repository, _, user = service_parts
    draft = await service.start(user)
    assert await service.start(user) is draft
    parsed = await service.submit(draft, "A: one\nB: two\nA: three\nB: four")
    assert parsed.message_count == 4
    await service.participant(draft, "A")
    await service.participant(draft, "A")
    await service.goal(draft, "What changed?")
    await service.goal(draft, "What changed?")
    assert await service.active(user.id) is draft
    await service.reset_conversation(draft)
    await service.reset_conversation(draft)
    assert draft.intake_step == "waiting_for_conversation"
    assert draft.normalized_conversation_json is None
    assert draft.participants_json is None
    assert draft.user_participant_label is None and draft.user_goal is None
    assert draft.relationship_stage is None and draft.message_count == draft.character_count == 0
    assert repository.items[draft.id] is draft


async def test_invalid_transitions_participant_goal_stage_and_cancellation(
    service_parts: tuple[ConversationIntakeService, MemoryAnalyses, RecordingAnalytics, User],
) -> None:
    service, _, analytics, user = service_parts
    draft = await service.start(user)
    with pytest.raises(InvalidTransition):
        await service.participant(draft, "A")
    await service.submit(draft, "A: one\nB: two\nA: three\nB: four")
    with pytest.raises(InvalidTransition):
        await service.participant(draft, "C")
    await service.participant(draft, "B")
    with pytest.raises(InvalidTransition):
        await service.goal(draft, "x" * 21)
    await service.goal(draft, "Question")
    with pytest.raises(InvalidTransition):
        await service.relationship_stage(draft, "invalid")
    await service.relationship_stage(draft, "not_provided")
    await service.relationship_stage(draft, "not_provided")
    assert draft.intake_step == "complete" and draft.status == "draft"
    with pytest.raises(InvalidTransition):
        await service.cancel(draft)
    with pytest.raises(InvalidTransition):
        await service.reset_conversation(draft)
    cancellable = await service.start(user)
    await service.cancel(cancellable)
    await service.cancel(cancellable)
    assert draft.intake_step == "complete" and draft.status == "draft"
    assert [event for event, _ in analytics.events].count("analysis_cancelled") == 1


async def test_ownership_and_analytics_never_expose_secret(
    service_parts: tuple[ConversationIntakeService, MemoryAnalyses, RecordingAnalytics, User],
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, analytics, user = service_parts
    draft = await service.start(user)
    assert await service.owned(draft.id, uuid4()) is None
    secret = "SECRET-MARKER"
    with pytest.raises(ConversationRejected):
        await service.submit(draft, f"A: {secret}")
    serialized = repr(analytics.events)
    assert secret not in serialized and secret not in caplog.text
    rejection = next(
        properties for event, properties in analytics.events if event == "conversation_rejected"
    )
    assert rejection == {
        "analysis_id": str(draft.id),
        "rejection_reason": "one_participant",
    }
