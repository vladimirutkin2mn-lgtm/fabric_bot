"""Telegram-only delivery of an already rendered report."""

import logging

from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import feedback_keyboard, report_actions_keyboard
from app.services.report_renderer import TELEGRAM_LIMIT, RenderedReport

logger = logging.getLogger(__name__)


async def deliver_report(
    message: Message,
    analysis_id: object,
    report: RenderedReport,
    *,
    feedback_exists: bool = False,
) -> None:
    """Deliver without changing durable analysis state when Telegram fails."""
    for index, chunk in enumerate(report.chunks):
        if not chunk or len(chunk) > TELEGRAM_LIMIT:
            raise ValueError("invalid_report_chunk")
        markup = report_actions_keyboard(analysis_id) if index == len(report.chunks) - 1 else None
        try:
            await message.answer(chunk, reply_markup=markup)
        except Exception:
            logger.warning(
                "report_delivery_failed analysis_id=%s stage=chunk chunk_index=%s "
                "error_category=telegram_send",
                analysis_id,
                index,
            )
            raise
    try:
        if feedback_exists:
            await message.answer(texts.FEEDBACK_ALREADY)
        else:
            await message.answer(texts.FEEDBACK_PROMPT, reply_markup=feedback_keyboard(analysis_id))
    except Exception:
        logger.warning(
            "report_delivery_failed analysis_id=%s stage=feedback chunk_index=none "
            "error_category=telegram_send",
            analysis_id,
        )
        raise
