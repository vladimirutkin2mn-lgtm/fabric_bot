"""Actual aiogram production checkout and receipt FSM handler tests."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TelegramUser

from app.bot.handlers import (
    buy_credits,
    cancel_receipt_contact,
    create_production_checkout,
    receive_receipt_contact,
)
from app.bot.states import PaymentStates
from app.config import Settings
from app.db.models import User
from app.providers.payments.base import Checkout
from app.services.checkout_service import CheckoutRejected, OneTimeCheckoutResult
from app.services.payment_service import CheckoutOutcome, CheckoutResult


class RecordingSession(AiohttpSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 -- aiogram session contract
    ) -> TelegramType:
        self.methods.append(method)
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
        if isinstance(method, AnswerCallbackQuery):
            return cast(TelegramType, True)
        return cast(TelegramType, True)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 -- aiogram session contract
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        if False:
            yield b""


class FakeOnboarding:
    def __init__(self) -> None:
        self.user = User(id=uuid4(), telegram_user_id=42, first_name="Buyer")

    async def current_user(self, telegram_user_id: int) -> User | None:
        return self.user if telegram_user_id == 42 else None


class FakeCheckout:
    def __init__(self, url: str | None = "https://provider.test/hosted", reject: bool = False):
        self.url, self.reject = url, reject
        self.calls: list[tuple[UUID, str, str, str, str | None]] = []

    async def create_one_time_checkout(
        self,
        user_id: UUID,
        product: str,
        market: str,
        currency: str,
        receipt_contact: str | None = None,
    ) -> OneTimeCheckoutResult:
        self.calls.append((user_id, product, market, currency, receipt_contact))
        if self.reject:
            raise CheckoutRejected("unavailable")
        return OneTimeCheckoutResult(user_id, uuid4(), self.url, "pending")


class FakeLegacyPayments:
    async def create_checkout(self, user_id: UUID, product: str) -> CheckoutResult:
        return CheckoutResult(
            CheckoutOutcome.CREATED,
            uuid4(),
            Checkout("mock", "mock-checkout", "https://local.test/mock"),
        )


@pytest.fixture
async def handler_context() -> AsyncGenerator[
    tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding], None
]:
    session = RecordingSession()
    bot = Bot("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", session=session)
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=bot.id, chat_id=42, user_id=42))
    actor = TelegramUser(id=42, is_bot=False, first_name="Buyer")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=actor,
        text="button",
    ).as_(bot)
    callback = CallbackQuery(
        id="callback",
        from_user=actor,
        chat_instance="chat",
        message=message,
        data="credits:offer:analysis_single:RU:RUB",
    ).as_(bot)
    yield bot, session, state, callback, FakeOnboarding()
    await bot.session.close()


def sent(session: RecordingSession) -> list[SendMessage]:
    return [method for method in session.methods if isinstance(method, SendMessage)]


def with_data(callback: CallbackQuery, data: str) -> CallbackQuery:
    return callback.model_copy(update={"data": data}).as_(callback.bot)


async def test_billing_disabled_uses_legacy_mock_checkout(
    handler_context: tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding],
    settings: Settings,
) -> None:
    _, session, _, callback, onboarding = handler_context
    callback = with_data(callback, "credits:buy:analysis_single")
    await buy_credits(callback, onboarding, FakeLegacyPayments(), settings)
    markup = cast(InlineKeyboardMarkup, sent(session)[-1].reply_markup)
    assert markup.inline_keyboard[0][0].url == "https://local.test/mock"


async def test_billing_enabled_opens_explicit_market_selection(
    handler_context: tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding],
    settings: Settings,
) -> None:
    _, session, _, callback, onboarding = handler_context
    callback = with_data(callback, "credits:buy:analysis_single")
    await buy_credits(
        callback, onboarding, None, settings.model_copy(update={"billing_enabled": True})
    )
    markup = cast(InlineKeyboardMarkup, sent(session)[-1].reply_markup)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks[:3] == [
        "credits:offer:analysis_single:RU:RUB",
        "credits:offer:analysis_single:INTERNATIONAL:EUR",
        "credits:offer:analysis_single:INTERNATIONAL:USD",
    ]


async def test_receipts_disabled_starts_direct_checkout_and_returns_button(
    handler_context: tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding],
    settings: Settings,
) -> None:
    _, session, state, callback, onboarding = handler_context
    checkout = FakeCheckout()
    await create_production_checkout(callback, state, onboarding, checkout, settings)
    assert checkout.calls[0][1:] == ("analysis_single", "RU", "RUB", None)
    assert sent(session)[-1].reply_markup.inline_keyboard[0][0].url == checkout.url  # type: ignore[union-attr]


async def test_receipts_enabled_stores_only_offer_coordinates(
    handler_context: tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding],
    settings: Settings,
) -> None:
    _, _, state, callback, onboarding = handler_context
    await create_production_checkout(
        callback,
        state,
        onboarding,
        FakeCheckout(),
        settings.model_copy(update={"yookassa_receipts_required": True}),
    )
    assert await state.get_state() == PaymentStates.waiting_for_receipt_contact.state
    assert await state.get_data() == {
        "product_code": "analysis_single",
        "market": "RU",
        "currency": "RUB",
    }


@pytest.mark.parametrize("contact", ["buyer@example.com", "+79991234567"])
async def test_valid_contact_starts_checkout_and_clears_fsm(
    handler_context: tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding],
    contact: str,
) -> None:
    bot, session, state, _, onboarding = handler_context
    await state.set_state(PaymentStates.waiting_for_receipt_contact)
    await state.set_data({"product_code": "analysis_single", "market": "RU", "currency": "RUB"})
    checkout = FakeCheckout()
    message = Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TelegramUser(id=42, is_bot=False, first_name="Buyer"),
        text=contact,
    ).as_(bot)
    await receive_receipt_contact(message, state, onboarding, checkout)
    assert checkout.calls[0][-1] == contact
    assert await state.get_state() is None and await state.get_data() == {}
    assert contact not in repr(sent(session))


async def test_invalid_contact_remains_in_state_for_retry(
    handler_context: tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding],
) -> None:
    bot, _, state, _, onboarding = handler_context
    await state.set_state(PaymentStates.waiting_for_receipt_contact)
    await state.set_data({"product_code": "analysis_single", "market": "RU", "currency": "RUB"})
    message = Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TelegramUser(id=42, is_bot=False, first_name="Buyer"),
        text="invalid",
    ).as_(bot)
    await receive_receipt_contact(message, state, onboarding, FakeCheckout())
    assert await state.get_state() == PaymentStates.waiting_for_receipt_contact.state


async def test_missing_text_and_cancel_callback_clear_state(
    handler_context: tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding],
) -> None:
    bot, _, state, callback, onboarding = handler_context
    await state.set_state(PaymentStates.waiting_for_receipt_contact)
    message = Message(
        message_id=3,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TelegramUser(id=42, is_bot=False, first_name="Buyer"),
    ).as_(bot)
    await receive_receipt_contact(message, state, onboarding, FakeCheckout())
    assert await state.get_state() is None
    await state.set_state(PaymentStates.waiting_for_receipt_contact)
    callback = with_data(callback, "credits:receipt:cancel")
    await cancel_receipt_contact(callback, state)
    assert await state.get_state() is None


@pytest.mark.parametrize(
    ("checkout", "expected"),
    [
        (FakeCheckout(reject=True), "Оплата сейчас недоступна"),
        (FakeCheckout(url=None), "Оплата создаётся"),
    ],
)
async def test_rejection_and_pending_checkout_clear_state(
    handler_context: tuple[Bot, RecordingSession, FSMContext, CallbackQuery, FakeOnboarding],
    checkout: FakeCheckout,
    expected: str,
) -> None:
    bot, session, state, _, onboarding = handler_context
    await state.set_state(PaymentStates.waiting_for_receipt_contact)
    await state.set_data({"product_code": "analysis_single", "market": "RU", "currency": "RUB"})
    message = Message(
        message_id=4,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TelegramUser(id=42, is_bot=False, first_name="Buyer"),
        text="buyer@example.com",
    ).as_(bot)
    await receive_receipt_contact(message, state, onboarding, checkout)
    assert await state.get_state() is None
    assert expected in sent(session)[-1].text
