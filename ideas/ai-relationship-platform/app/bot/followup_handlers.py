"""Telegram intake and replay flow for the one included paid follow-up."""
# ruff: noqa: RUF001

from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.followup_service import FollowUpResult, FollowUpService, FollowUpStatus, FollowUpView
from app.services.onboarding import OnboardingService

router = Router(name="followups")


class FollowUpStates(StatesGroup):
    waiting_for_question = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отменить", callback_data="followup:cancel")],
            [InlineKeyboardButton(text="Главное меню", callback_data="report:menu")],
        ]
    )


def _done_keyboard(analysis_id: UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть полный отчёт",
                    callback_data=f"history:open:{analysis_id}",
                )
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="report:menu")],
        ]
    )


async def _deliver(message: Message, view: FollowUpView) -> None:
    await message.answer(f"Ваш уточняющий вопрос:\n{view.question}")
    text = f"Ответ:\n{view.answer}"
    if view.limitations:
        text += "\n\nЧто важно учитывать:\n" + "\n".join(
            f"• {item}" for item in view.limitations
        )
    if view.safety_high_risk:
        text += "\n\nВ ответе отдельно учтены риски безопасности."
    await message.answer(text, reply_markup=_done_keyboard(view.analysis_id))


def _analysis_id(callback: CallbackQuery) -> UUID | None:
    try:
        return UUID((callback.data or "").split(":")[-1])
    except ValueError:
        return None


@router.callback_query(F.data.startswith("report:followup:"))
async def open_followup(
    callback: CallbackQuery,
    state: FSMContext,
    onboarding: OnboardingService,
    followups: FollowUpService,
) -> None:
    analysis_id = _analysis_id(callback)
    user = await onboarding.current_user(callback.from_user.id)
    await callback.answer()
    if analysis_id is None or user is None or not isinstance(callback.message, Message):
        return
    result = await followups.inspect(analysis_id, user.id)
    if result.status is FollowUpStatus.COMPLETED and result.view is not None:
        await state.clear()
        await _deliver(callback.message, result.view)
        return
    if result.status is FollowUpStatus.PROCESSING:
        await state.clear()
        await callback.message.answer(
            "Уточняющий вопрос уже обрабатывается. Откройте этот разбор позже, чтобы увидеть ответ."
        )
        return
    if result.status is FollowUpStatus.READY:
        await state.set_state(FollowUpStates.waiting_for_question)
        await state.set_data({"followup_analysis_id": str(analysis_id)})
        await callback.message.answer(
            "Полный отчёт включает один уточняющий вопрос. Напишите его одним сообщением — "
            "до 1000 символов. После успешного ответа право будет использовано.",
            reply_markup=_cancel_keyboard(),
        )
        return
    if result.status is FollowUpStatus.CORRUPTED_HISTORY:
        await callback.message.answer(
            "Сохранённый ответ повреждён и недоступен. Обратитесь в поддержку."
        )
        return
    await state.clear()
    await callback.message.answer(
        "Уточняющий вопрос доступен только для вашей завершённой оплаченной полной аналитики."
    )


@router.message(FollowUpStates.waiting_for_question)
async def receive_followup(
    message: Message,
    state: FSMContext,
    onboarding: OnboardingService,
    followups: FollowUpService,
) -> None:
    data = await state.get_data()
    if message.from_user is None or not message.text:
        await message.answer(
            "Отправьте вопрос обычным текстовым сообщением.",
            reply_markup=_cancel_keyboard(),
        )
        return
    try:
        analysis_id = UUID(str(data["followup_analysis_id"]))
    except (KeyError, ValueError):
        await state.clear()
        await message.answer("Сессия устарела. Откройте полный отчёт заново.")
        return
    user = await onboarding.current_user(message.from_user.id)
    if user is None:
        await state.clear()
        return
    await message.answer("Готовлю ответ на уточняющий вопрос…")
    result = await followups.ask(analysis_id, user.id, message.text)
    await _finish_message(message, state, result)


async def _finish_message(message: Message, state: FSMContext, result: FollowUpResult) -> None:
    if result.status is FollowUpStatus.INVALID_QUESTION:
        await message.answer(
            "Вопрос пустой или длиннее 1000 символов. Сократите его и отправьте снова.",
            reply_markup=_cancel_keyboard(),
        )
        return
    await state.clear()
    if result.status is FollowUpStatus.COMPLETED and result.view is not None:
        await _deliver(message, result.view)
    elif result.status is FollowUpStatus.PROCESSING:
        await message.answer(
            "Этот вопрос уже обрабатывается. Откройте разбор позже — повторный LLM-вызов не нужен."
        )
    elif result.status is FollowUpStatus.FAILED_RELEASED:
        await message.answer(
            "Не удалось подготовить ответ из-за технической ошибки. Право на уточняющий вопрос "
            "сохранено — попробуйте снова из полного отчёта."
        )
    elif result.status is FollowUpStatus.CORRUPTED_HISTORY:
        await message.answer("Сохранённый ответ повреждён и недоступен.")
    else:
        await message.answer(
            "Уточняющий вопрос больше недоступен для этого разбора или разбор был удалён."
        )


@router.callback_query(F.data == "followup:cancel")
async def cancel_followup(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Уточняющий вопрос отменён. Право на него не использовано.")
