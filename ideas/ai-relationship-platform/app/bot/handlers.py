"""Telegram onboarding and Milestone 1 navigation handlers."""

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.keyboards import age_keyboard, consent_keyboard, main_menu_keyboard
from app.bot.states import OnboardingStates
from app.services.onboarding import (
    CURRENT_CONSENT_VERSION,
    OnboardingService,
    OnboardingStep,
    TelegramIdentity,
)

router = Router(name="onboarding")


def identity_from_callback(callback: CallbackQuery) -> TelegramIdentity:
    telegram_user = callback.from_user
    return TelegramIdentity(
        telegram_user_id=telegram_user.id,
        username=telegram_user.username,
        first_name=telegram_user.first_name,
        language=telegram_user.language_code,
    )


async def show_step(message: Message, state: FSMContext, step: OnboardingStep) -> None:
    if step is OnboardingStep.AGE:
        await state.set_state(OnboardingStates.waiting_for_age)
        await message.answer(texts.WELCOME, reply_markup=age_keyboard())
    elif step is OnboardingStep.CONSENT:
        await state.set_state(OnboardingStates.waiting_for_consent)
        await message.answer(
            texts.CONSENT.format(version=CURRENT_CONSENT_VERSION),
            reply_markup=consent_keyboard(),
        )
    else:
        await state.clear()
        await message.answer(texts.MAIN_MENU, reply_markup=main_menu_keyboard())


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, onboarding: OnboardingService) -> None:
    if message.from_user is None:
        return
    telegram_user = message.from_user
    _, step = await onboarding.start(
        TelegramIdentity(
            telegram_user_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            language=telegram_user.language_code,
        )
    )
    await show_step(message, state, step)


@router.callback_query(F.data == "onboarding:age:no")
async def decline_age(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.AGE_DECLINED)


@router.callback_query(F.data == "onboarding:age:yes")
async def confirm_age(
    callback: CallbackQuery, state: FSMContext, onboarding: OnboardingService
) -> None:
    if await onboarding.current_step(callback.from_user.id) is OnboardingStep.AGE:
        await onboarding.start(identity_from_callback(callback))
    step = await onboarding.confirm_age(callback.from_user.id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await show_step(callback.message, state, step)


@router.callback_query(F.data == "onboarding:consent")
async def accept_consent(
    callback: CallbackQuery, state: FSMContext, onboarding: OnboardingService
) -> None:
    if await onboarding.current_step(callback.from_user.id) is OnboardingStep.AGE:
        await onboarding.start(identity_from_callback(callback))
    step = await onboarding.accept_consent(callback.from_user.id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await show_step(callback.message, state, step)


@router.callback_query(F.data == "menu:analyze")
async def analyze(
    callback: CallbackQuery, state: FSMContext, onboarding: OnboardingService
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if await onboarding.analysis_allowed(callback.from_user.id):
        await callback.message.answer(texts.COMING_LATER)
        return
    step = await onboarding.current_step(callback.from_user.id)
    await show_step(callback.message, state, step)


@router.callback_query(F.data.in_({"menu:history", "menu:balance", "menu:privacy"}))
async def placeholder(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.COMING_LATER)
