"""Telegram-only delivery of an already rendered report."""

from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import feedback_keyboard, report_actions_keyboard
from app.services.report_renderer import RenderedReport


async def deliver_report(
    message: Message, analysis_id: object, report: RenderedReport, *, feedback_exists: bool = False
) -> None:
    for index, chunk in enumerate(report.chunks):
        markup = report_actions_keyboard(analysis_id) if index == len(report.chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)
    if feedback_exists:
        await message.answer(texts.FEEDBACK_ALREADY)
    else:
        await message.answer(texts.FEEDBACK_PROMPT, reply_markup=feedback_keyboard(analysis_id))
