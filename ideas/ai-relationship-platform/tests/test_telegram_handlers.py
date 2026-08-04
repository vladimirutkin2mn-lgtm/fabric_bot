"""Handler tests through aiogram's real Dispatcher without Telegram network calls."""

from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update
from aiogram.types import User as TelegramUser

from app.bot import texts
from app.bot.handlers import privacy_screen, router, start
from app.bot.rate_limit import FixedWindowRateLimiter, RateLimitMiddleware
from app.db.models import Analysis, User
from app.domain.analysis import AnalysisResult
from app.services.analysis_service import AnalysisServiceResult, AnalysisServiceStatus
from app.services.conversation_intake import ConversationIntakeService
from app.services.conversation_parser import ConversationParser
from app.services.onboarding import CURRENT_CONSENT_VERSION, OnboardingService, TelegramIdentity
from app.services.preview_entitlement import PreviewState
from app.services.report_renderer import ReportRenderer
from app.services.report_service import ReportRepository, ReportService


class CompletedRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID]] = []

    async def analyze(self, analysis_id: UUID, user_id: UUID) -> AnalysisServiceResult:
        self.calls.append((analysis_id, user_id))
        payload = {
            "quality": {"sufficient": True, "issues": [], "participants_detected": ["A", "B"]},
            "summary": "Тестовый вывод.",
            "dynamic": {"direction": "mixed", "confidence": 0.6},
            "reciprocity_score": {
                "value": 50,
                "positive_signals": [],
                "negative_signals": [],
                "limitations": [],
            },
            "observations": [
                {"claim": "Диалог продолжается.", "evidence_refs": ["m1"], "importance": "medium"}
            ],
            "hypotheses": [],
            "unknowns": [],
            "next_actions": [],
            "reply_suggestions": [],
            "safety": {"high_risk_detected": False, "categories": []},
        }
        import json

        return AnalysisServiceResult(
            AnalysisServiceStatus.COMPLETED, AnalysisResult.model_validate_json(json.dumps(payload))
        )


type Harness = tuple[Dispatcher, Bot, "RecordingSession", "MemoryUsers", OnboardingService]


class RecordingSession(AiohttpSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list[TelegramMethod[Any]] = []
        self.fail_text: str | None = None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 -- aiogram API
    ) -> TelegramType:
        self.methods.append(method)
        if isinstance(method, SendMessage) and method.text == self.fail_text:
            self.fail_text = None
            raise RuntimeError("SECRET-PRIVATE-CONTENT")
        if isinstance(method, SendMessage):
            return cast(
                TelegramType,
                Message(
                    message_id=len(self.methods) + 100,
                    date=datetime.now(UTC),
                    chat=Chat(id=int(method.chat_id), type="private"),
                    text=method.text,
                ),
            )
        return cast(TelegramType, True)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 -- aiogram API
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        if False:  # pragma: no cover - required async-generator shape
            yield b""


class MemoryUsers:
    def __init__(self) -> None:
        self.users: dict[int, User] = {}

    async def get_or_create(
        self, telegram_user_id: int, username: str | None, first_name: str, language: str | None
    ) -> tuple[User, bool]:
        existing = self.users.get(telegram_user_id)
        if existing:
            return existing, False
        user = User(
            id=uuid4(),
            telegram_user_id=telegram_user_id,
            telegram_username=username,
            first_name=first_name,
            telegram_language=language,
            age_confirmed=False,
            onboarding_completed=False,
        )
        self.users[telegram_user_id] = user
        return user, True

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        return self.users.get(telegram_user_id)

    async def save(self, user: User) -> None:
        assert user.telegram_user_id is not None
        self.users[user.telegram_user_id] = user


class NoOpAnalytics:
    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        pass


class FakeCredits:
    async def balance(self, user_id: UUID) -> int:
        return 0


class FakePreviews:
    async def get_preview_state(self, user_id: UUID) -> PreviewState:
        return PreviewState("available", None)


class MemoryAnalyses:
    def __init__(self) -> None:
        self.analyses: dict[UUID, Analysis] = {}

    async def create_or_resume(self, user_id: UUID) -> tuple[Analysis, bool]:
        active = await self.get_active(user_id)
        if active is not None:
            return active, False
        analysis = Analysis(
            id=uuid4(),
            user_id=user_id,
            status="draft",
            intake_step="waiting_for_conversation",
            source_type="text",
            message_count=0,
            character_count=0,
        )
        self.analyses[analysis.id] = analysis
        return analysis, True

    async def get_active(self, user_id: UUID) -> Analysis | None:
        return next(
            (
                item
                for item in self.analyses.values()
                if item.user_id == user_id
                and item.status == "draft"
                and item.intake_step != "complete"
            ),
            None,
        )

    async def get_latest_pending_billing(self, user_id: UUID) -> Analysis | None:
        return next(
            (
                item
                for item in reversed(tuple(self.analyses.values()))
                if item.user_id == user_id
                and item.status == "draft"
                and item.intake_step == "complete"
                and item.report_access in {None, "none"}
            ),
            None,
        )

    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None:
        analysis = self.analyses.get(analysis_id)
        return analysis if analysis is not None and analysis.user_id == user_id else None

    async def save(self, analysis: Analysis) -> None:
        self.analyses[analysis.id] = analysis

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


dispatcher = Dispatcher()
dispatcher.include_router(router)


@pytest.fixture
async def harness() -> AsyncGenerator[Harness, None]:
    session = RecordingSession()
    bot = Bot("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", session=session)
    users = MemoryUsers()
    service = OnboardingService(users, NoOpAnalytics())
    analyses = MemoryAnalyses()
    dispatcher["intake"] = ConversationIntakeService(
        analyses, ConversationParser(), NoOpAnalytics()
    )
    dispatcher["analysis_service"] = CompletedRunner()
    dispatcher["credits"] = FakeCredits()
    dispatcher["previews"] = FakePreviews()
    dispatcher["analysis_price"] = 1
    dispatcher["reports"] = ReportService(
        cast(ReportRepository, analyses), ReportRenderer(), NoOpAnalytics()
    )
    yield dispatcher, bot, session, users, service
    await bot.session.close()


def telegram_user(user_id: int = 42) -> TelegramUser:
    return TelegramUser(
        id=user_id, is_bot=False, first_name="Анна", username="anna", language_code="ru"
    )


def start_update(update_id: int = 1, user_id: int = 42) -> Update:
    user = telegram_user(user_id)
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            text="/start",
            entities=[MessageEntity(type="bot_command", offset=0, length=6)],
        ),
    )


def callback_update(data: str, update_id: int = 2, user_id: int = 42) -> Update:
    user = telegram_user(user_id)
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"callback-{update_id}",
            from_user=user,
            chat_instance="test",
            data=data,
            message=Message(
                message_id=10,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                from_user=user,
                text="button",
            ),
        ),
    )


def message_update(text: str | None, update_id: int, user_id: int = 42) -> Update:
    user = telegram_user(user_id)
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            text=text,
            photo=[] if text is None else None,
        ),
    )


def sent_texts(session: RecordingSession) -> list[str]:
    return [method.text for method in session.methods if isinstance(method, SendMessage)]


async def complete(service: OnboardingService, user_id: int = 42) -> None:
    await service.start(TelegramIdentity(user_id, "anna", "Анна", "ru"))
    await service.confirm_age(user_id)
    await service.accept_consent(user_id)


async def test_new_and_repeated_start_ask_for_age_without_duplicate(harness: Harness) -> None:
    dispatcher, bot, session, users, service = harness
    await dispatcher.feed_update(bot, start_update(), onboarding=service)
    await dispatcher.feed_update(bot, start_update(2), onboarding=service)
    assert sent_texts(session) == [texts.WELCOME, texts.WELCOME]
    assert len(users.users) == 1


async def test_age_confirmation_shows_consent(harness: Harness) -> None:
    dispatcher, bot, session, _, service = harness
    await dispatcher.feed_update(bot, start_update(), onboarding=service)
    await dispatcher.feed_update(bot, callback_update("onboarding:age:yes"), onboarding=service)
    assert sent_texts(session)[-1] == texts.CONSENT.format(version=CURRENT_CONSENT_VERSION)


async def test_age_decline_clears_fsm_and_shows_restriction(harness: Harness) -> None:
    dispatcher, bot, session, _, service = harness
    await dispatcher.feed_update(bot, start_update(), onboarding=service)
    await dispatcher.feed_update(bot, callback_update("onboarding:age:no"), onboarding=service)
    context = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    assert await context.get_state() is None
    assert sent_texts(session)[-1] == texts.AGE_DECLINED


async def test_consent_acceptance_and_completed_start_show_menu(harness: Harness) -> None:
    dispatcher, bot, session, _, service = harness
    await dispatcher.feed_update(bot, start_update(), onboarding=service)
    await dispatcher.feed_update(bot, callback_update("onboarding:age:yes"), onboarding=service)
    await dispatcher.feed_update(bot, callback_update("onboarding:consent", 3), onboarding=service)
    await dispatcher.feed_update(bot, start_update(4), onboarding=service)
    assert sent_texts(session)[-2:] == [texts.MAIN_MENU, texts.MAIN_MENU]


async def test_analyze_without_current_consent_returns_to_onboarding(harness: Harness) -> None:
    dispatcher, bot, session, users, service = harness
    await complete(service)
    users.users[42].consent_version = "obsolete"
    await dispatcher.feed_update(bot, callback_update("menu:analyze"), onboarding=service)
    assert sent_texts(session)[-1] == texts.CONSENT.format(version=CURRENT_CONSENT_VERSION)
    assert not users.users[42].onboarding_completed


async def test_analyze_with_current_consent_shows_placeholder(harness: Harness) -> None:
    dispatcher, bot, session, _, service = harness
    await complete(service)
    await dispatcher.feed_update(bot, callback_update("menu:analyze"), onboarding=service)
    assert sent_texts(session)[-1] == texts.CONVERSATION_INSTRUCTIONS


async def test_complete_intake_duplicate_callbacks_and_restart_resume(harness: Harness) -> None:
    dispatcher, bot, session, users, service = harness
    await complete(service)
    intake = cast(ConversationIntakeService, dispatcher["intake"])
    await dispatcher.feed_update(
        bot, callback_update("menu:analyze", 10), onboarding=service, intake=intake
    )
    draft = await intake.active(users.users[42].id)
    assert draft is not None
    await dispatcher.feed_update(
        bot,
        message_update("A: one\nB: two\nA: three\nB: four", 11),
        onboarding=service,
        intake=intake,
    )
    participant = f"intake:participant:{draft.id}:A"
    await dispatcher.feed_update(
        bot, callback_update(participant, 12), onboarding=service, intake=intake
    )
    await dispatcher.feed_update(
        bot, callback_update(participant, 13), onboarding=service, intake=intake
    )
    goal = f"intake:goal:{draft.id}:0"
    await dispatcher.feed_update(bot, callback_update(goal, 14), onboarding=service, intake=intake)
    await dispatcher.feed_update(bot, callback_update(goal, 15), onboarding=service, intake=intake)
    stage = f"intake:stage:{draft.id}:not_provided"
    await dispatcher.feed_update(bot, callback_update(stage, 16), onboarding=service, intake=intake)
    await dispatcher.feed_update(bot, callback_update(stage, 17), onboarding=service, intake=intake)
    assert draft.intake_step == "complete" and draft.relationship_stage == "not_provided"
    assert texts.PROCESSING not in sent_texts(session)
    assert "Полный отчёт: 1 кредитов" in " ".join(sent_texts(session))

    second = await intake.start(users.users[42])
    await intake.submit(second, "A: 1\nB: 2\nA: 3\nB: 4")
    await dispatcher.feed_update(
        bot, callback_update(f"intake:reset:{draft.id}", 18), onboarding=service, intake=intake
    )
    await dispatcher.feed_update(
        bot, callback_update(f"intake:cancel:{draft.id}", 19), onboarding=service, intake=intake
    )
    assert draft.intake_step == "complete" and draft.status == "draft"
    assert second.intake_step == "waiting_for_participant"
    await dispatcher.feed_update(bot, start_update(20), onboarding=service, intake=intake)
    assert sent_texts(session)[-1] == texts.PARTICIPANT_QUESTION


async def test_invalid_non_text_reset_cancel_menu_and_stale_callbacks(harness: Harness) -> None:
    dispatcher, bot, session, users, service = harness
    await complete(service)
    intake = cast(ConversationIntakeService, dispatcher["intake"])
    await dispatcher.feed_update(
        bot, callback_update("menu:analyze", 20), onboarding=service, intake=intake
    )
    draft = await intake.active(users.users[42].id)
    assert draft is not None
    await dispatcher.feed_update(bot, message_update(None, 21), onboarding=service, intake=intake)
    assert sent_texts(session)[-1] == texts.TEXT_ONLY
    await dispatcher.feed_update(
        bot, message_update("A: secret", 22), onboarding=service, intake=intake
    )
    assert sent_texts(session)[-1] == texts.REJECTION_MESSAGES["one_participant"]
    await dispatcher.feed_update(
        bot, message_update("A: 1\nB: 2\nA: 3\nB: 4", 23), onboarding=service, intake=intake
    )
    reset = f"intake:reset:{draft.id}"
    await dispatcher.feed_update(bot, callback_update(reset, 24), onboarding=service, intake=intake)
    await dispatcher.feed_update(bot, callback_update(reset, 25), onboarding=service, intake=intake)
    assert draft.intake_step == "waiting_for_conversation" and draft.message_count == 0
    await dispatcher.feed_update(
        bot, callback_update(f"intake:menu:{draft.id}", 26), onboarding=service, intake=intake
    )
    assert sent_texts(session)[-1] == texts.MAIN_MENU
    cancel = f"intake:cancel:{draft.id}"
    await dispatcher.feed_update(
        bot, callback_update(cancel, 27), onboarding=service, intake=intake
    )
    await dispatcher.feed_update(
        bot, callback_update(cancel, 28), onboarding=service, intake=intake
    )
    assert draft.status == "deleted"
    await dispatcher.feed_update(
        bot,
        callback_update(f"intake:participant:{uuid4()}:A", 29),
        onboarding=service,
        intake=intake,
    )
    alerts = [method for method in session.methods if isinstance(method, AnswerCallbackQuery)]
    assert alerts[-1].show_alert


async def test_custom_goal_text_and_non_text(harness: Harness) -> None:
    dispatcher, bot, session, users, service = harness
    await complete(service)
    intake = cast(ConversationIntakeService, dispatcher["intake"])
    draft = await intake.start(users.users[42])
    await intake.submit(draft, "A: 1\nB: 2\nA: 3\nB: 4")
    await intake.participant(draft, "B")
    custom = f"intake:goal:{draft.id}:custom"
    await dispatcher.feed_update(
        bot, callback_update(custom, 30), onboarding=service, intake=intake
    )
    await dispatcher.feed_update(bot, message_update(None, 31), onboarding=service, intake=intake)
    assert sent_texts(session)[-1] == texts.CUSTOM_GOAL_TEXT_ONLY
    await dispatcher.feed_update(
        bot, message_update("Мой вопрос", 32), onboarding=service, intake=intake
    )
    assert draft.user_goal == "Мой вопрос"
    await dispatcher.feed_update(
        bot,
        callback_update(f"intake:stage:{draft.id}:dating", 33),
        onboarding=service,
        intake=intake,
    )
    assert draft.relationship_stage == "dating"


@pytest.mark.parametrize("data", ["onboarding:age:yes", "onboarding:consent"])
async def test_onboarding_callback_without_user_is_handled(harness: Harness, data: str) -> None:
    dispatcher, bot, session, users, service = harness
    await dispatcher.feed_update(bot, callback_update(data), onboarding=service)
    assert 42 in users.users
    assert any(isinstance(method, AnswerCallbackQuery) for method in session.methods)


async def test_rate_limit_middleware_applies_to_start_and_callbacks() -> None:
    limited_dispatcher = Dispatcher()
    limited_dispatcher.message.outer_middleware(
        RateLimitMiddleware(FixedWindowRateLimiter(limit=1))
    )
    limited_dispatcher.callback_query.outer_middleware(
        RateLimitMiddleware(FixedWindowRateLimiter(limit=1))
    )
    rate_router = Router()
    rate_router.message.register(start, CommandStart())
    rate_router.callback_query.register(privacy_screen, F.data == "menu:history")
    limited_dispatcher.include_router(rate_router)
    session = RecordingSession()
    bot = Bot("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", session=session)
    users = MemoryUsers()
    service = OnboardingService(users, NoOpAnalytics())
    intake = ConversationIntakeService(MemoryAnalyses(), ConversationParser(), NoOpAnalytics())
    billing = {"credits": FakeCredits(), "previews": FakePreviews(), "analysis_price": 1}
    await limited_dispatcher.feed_update(
        bot, start_update(), onboarding=service, intake=intake, **billing
    )
    await limited_dispatcher.feed_update(
        bot, start_update(2), onboarding=service, intake=intake, **billing
    )
    await limited_dispatcher.feed_update(
        bot, callback_update("menu:history", 3), onboarding=service, privacy_retention_days=30
    )
    await limited_dispatcher.feed_update(
        bot, callback_update("menu:history", 4), onboarding=service, privacy_retention_days=30
    )
    assert sent_texts(session).count(texts.RATE_LIMITED) == 1
    alerts = [method for method in session.methods if isinstance(method, AnswerCallbackQuery)]
    assert any(method.text == texts.RATE_LIMITED and method.show_alert for method in alerts)
    await bot.session.close()


async def test_processing_notice_failure_still_runs_analysis_privately(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    dispatcher, bot, session, users, service = harness
    await complete(service)
    intake = cast(ConversationIntakeService, dispatcher["intake"])
    runner = CompletedRunner()
    dispatcher["analysis_service"] = runner
    await dispatcher.feed_update(
        bot, callback_update("menu:analyze", 90), onboarding=service, intake=intake
    )
    draft = await intake.active(users.users[42].id)
    assert draft is not None
    await dispatcher.feed_update(
        bot,
        message_update("A: one\nB: two\nA: three\nB: four", 91),
        onboarding=service,
        intake=intake,
    )
    await dispatcher.feed_update(
        bot,
        callback_update(f"intake:participant:{draft.id}:A", 92),
        onboarding=service,
        intake=intake,
    )
    await dispatcher.feed_update(
        bot, callback_update(f"intake:goal:{draft.id}:0", 93), onboarding=service, intake=intake
    )
    await dispatcher.feed_update(
        bot,
        callback_update(f"intake:stage:{draft.id}:dating", 94),
        onboarding=service,
        intake=intake,
    )
    assert runner.calls == []
    assert "Полный отчёт: 1 кредитов" in " ".join(sent_texts(session))
    assert "SECRET-PRIVATE-CONTENT" not in caplog.text
