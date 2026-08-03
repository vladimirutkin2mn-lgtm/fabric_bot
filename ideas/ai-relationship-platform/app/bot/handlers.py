"""Telegram onboarding and conversation intake handlers."""
# ruff: noqa: RUF001

from uuid import UUID

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.keyboards import (
    age_keyboard,
    cancel_keyboard,
    consent_keyboard,
    goal_keyboard,
    main_menu_keyboard,
    participant_keyboard,
    stage_keyboard,
)
from app.bot.states import IntakeStates, OnboardingStates
from app.db.models import Analysis
from app.services.conversation_intake import ConversationIntakeService, InvalidTransition
from app.services.conversation_parser import ConversationRejected
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
async def start(
    message: Message,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
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
    if step is OnboardingStep.COMPLETE:
        user = await onboarding.current_user(telegram_user.id)
        analysis = None if user is None else await intake.active(user.id)
        if analysis is not None:
            await show_intake_step(message, state, analysis)
            return
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
    callback: CallbackQuery,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if await onboarding.analysis_allowed(callback.from_user.id):
        user = await onboarding.current_user(callback.from_user.id)
        if user is None:
            await callback.message.answer(texts.STALE_DRAFT)
            return
        analysis = await intake.start(user)
        await show_intake_step(callback.message, state, analysis)
        return
    step = await onboarding.current_step(callback.from_user.id)
    await show_step(callback.message, state, step)


async def show_intake_step(message: Message, state: FSMContext, analysis: Analysis) -> None:
    step = analysis.intake_step
    if step == "waiting_for_conversation":
        await state.set_state(IntakeStates.waiting_for_conversation)
        await message.answer(
            texts.CONVERSATION_INSTRUCTIONS, reply_markup=cancel_keyboard(analysis.id)
        )
    elif step == "waiting_for_participant":
        await state.clear()
        await message.answer(
            texts.PARTICIPANT_QUESTION,
            reply_markup=participant_keyboard(analysis.id, analysis.participants_json or {}),
        )
    elif step == "waiting_for_goal":
        await state.clear()
        await message.answer(texts.GOAL_QUESTION, reply_markup=goal_keyboard(analysis.id))
    elif step == "waiting_for_relationship_stage":
        await state.clear()
        await message.answer(texts.STAGE_QUESTION, reply_markup=stage_keyboard(analysis.id))
    else:
        await state.clear()
        await message.answer(texts.DRAFT_READY, reply_markup=main_menu_keyboard())


async def _owned(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
    analysis_id: str,
) -> Analysis | None:
    user = await onboarding.current_user(callback.from_user.id)
    try:
        parsed_id = UUID(analysis_id)
    except ValueError:
        return None
    return None if user is None else await intake.owned(parsed_id, user.id)


def _callback_parts(callback: CallbackQuery) -> list[str]:
    return (callback.data or "").split(":")


@router.message(IntakeStates.waiting_for_conversation)
async def receive_conversation(
    message: Message,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
    if message.from_user is None:
        return
    user = await onboarding.current_user(message.from_user.id)
    analysis = None if user is None else await intake.active(user.id)
    if analysis is None:
        await message.answer(texts.STALE_DRAFT)
    elif not message.text:
        await message.answer(texts.TEXT_ONLY, reply_markup=cancel_keyboard(analysis.id))
    else:
        try:
            parsed = await intake.submit(analysis, message.text)
        except ConversationRejected as error:
            await message.answer(
                texts.REJECTION_MESSAGES[error.reason.value],
                reply_markup=cancel_keyboard(analysis.id),
            )
        else:
            await state.clear()
            await message.answer(
                texts.PARTICIPANT_QUESTION,
                reply_markup=participant_keyboard(analysis.id, parsed.participants),
            )


@router.callback_query(F.data.startswith("intake:participant:"))
async def choose_participant(
    callback: CallbackQuery,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
    parts = _callback_parts(callback)
    analysis = await _owned(callback, onboarding, intake, parts[2] if len(parts) > 2 else "")
    try:
        if analysis is None:
            raise InvalidTransition("missing")
        await intake.participant(analysis, parts[3] if len(parts) > 3 else "")
    except InvalidTransition:
        await callback.answer(texts.STALE_DRAFT, show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.GOAL_QUESTION, reply_markup=goal_keyboard(analysis.id))


GOALS = [
    "Есть ли у человека интерес?",
    "Общение стало холоднее?",
    "Стоит ли написать сейчас?",
    "Как лучше ответить?",
    "Что изменилось?",
]


@router.callback_query(F.data.startswith("intake:goal:"))
async def choose_goal(
    callback: CallbackQuery,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
    parts = _callback_parts(callback)
    value = parts[3] if len(parts) > 3 else ""
    analysis = await _owned(callback, onboarding, intake, parts[2] if len(parts) > 2 else "")
    if analysis is None:
        await callback.answer(texts.STALE_DRAFT, show_alert=True)
        return
    if value == "custom":
        await state.set_state(IntakeStates.waiting_for_goal)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Напишите свой вопрос одним сообщением.", reply_markup=cancel_keyboard(analysis.id)
            )
        return
    try:
        if analysis is None:
            raise InvalidTransition("missing")
        await intake.goal(analysis, GOALS[int(value)])
    except (InvalidTransition, ValueError, IndexError):
        await callback.answer(texts.STALE_DRAFT, show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            texts.STAGE_QUESTION, reply_markup=stage_keyboard(analysis.id)
        )


@router.message(IntakeStates.waiting_for_goal)
async def custom_goal(
    message: Message,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
    if message.from_user is None:
        return
    user = await onboarding.current_user(message.from_user.id)
    analysis = None if user is None else await intake.active(user.id)
    if analysis is not None and not message.text:
        await message.answer(texts.CUSTOM_GOAL_TEXT_ONLY, reply_markup=cancel_keyboard(analysis.id))
        return
    try:
        if analysis is None:
            raise InvalidTransition("missing")
        await intake.goal(analysis, message.text or "")
    except InvalidTransition:
        await message.answer("Вопрос пустой или слишком длинный. Сократите его и отправьте снова.")
        return
    await state.clear()
    await message.answer(texts.STAGE_QUESTION, reply_markup=stage_keyboard(analysis.id))


@router.callback_query(F.data.startswith("intake:stage:"))
async def choose_stage(
    callback: CallbackQuery,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
    parts = _callback_parts(callback)
    analysis = await _owned(callback, onboarding, intake, parts[2] if len(parts) > 2 else "")
    try:
        if analysis is None:
            raise InvalidTransition("missing")
        await intake.relationship_stage(analysis, parts[3] if len(parts) > 3 else "")
    except InvalidTransition:
        await callback.answer(texts.STALE_DRAFT, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.DRAFT_READY, reply_markup=main_menu_keyboard())


@router.callback_query(F.data.startswith("intake:cancel:"))
async def cancel_intake(
    callback: CallbackQuery,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
    parts = _callback_parts(callback)
    analysis = await _owned(callback, onboarding, intake, parts[2] if len(parts) > 2 else "")
    if analysis is not None:
        await intake.cancel(analysis)
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.CANCELLED, reply_markup=main_menu_keyboard())


@router.callback_query(F.data.startswith("intake:reset:"))
async def resend(
    callback: CallbackQuery,
    state: FSMContext,
    onboarding: OnboardingService,
    intake: ConversationIntakeService,
) -> None:
    parts = _callback_parts(callback)
    analysis = await _owned(callback, onboarding, intake, parts[2] if len(parts) > 2 else "")
    if analysis is None:
        await callback.answer(texts.STALE_DRAFT, show_alert=True)
        return
    await intake.reset_conversation(analysis)
    await callback.answer()
    if isinstance(callback.message, Message):
        await state.set_state(IntakeStates.waiting_for_conversation)
        await callback.message.answer(
            texts.CONVERSATION_INSTRUCTIONS, reply_markup=cancel_keyboard(analysis.id)
        )


@router.callback_query(F.data.startswith("intake:menu:"))
async def return_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.MAIN_MENU, reply_markup=main_menu_keyboard())


@router.callback_query(F.data.in_({"menu:history", "menu:balance", "menu:privacy"}))
async def placeholder(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.COMING_LATER)
