"""Telegram flow for safe monetary refund requests."""
# ruff: noqa: RUF001

from decimal import Decimal
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.onboarding import OnboardingService
from app.services.refund_service import (
    RefundPurchaseView,
    RefundRequestOutcome,
    RefundService,
    RefundView,
)

router = Router(name="refunds")

_STATUS_LABELS = {
    "requested": "запрошен",
    "credits_reserved": "кредиты зарезервированы",
    "provider_pending": "обрабатывается платёжной системой",
    "succeeded": "деньги возвращены",
    "failed": "возврат отклонён",
    "manual_review": "нужна ручная проверка",
}


def _money(amount_minor: int, currency: str) -> str:
    return f"{Decimal(amount_minor) / Decimal(100):.2f} {currency}"


def _purchase_keyboard(rows: tuple[RefundPurchaseView, ...]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=(
                    f"{row.product_code}: {row.refundable_credits} кр. · "
                    f"{_money(row.refund_amount_minor, row.currency)}"
                ),
                callback_data=(f"refund:request:{row.payment_order_id}:{row.refundable_credits}"),
            )
        ]
        for row in rows
    ]
    buttons.append([InlineKeyboardButton(text="История возвратов", callback_data="refund:history")])
    buttons.append([InlineKeyboardButton(text="Вернуться", callback_data="menu:balance")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _history_text(rows: tuple[RefundView, ...]) -> str:
    if not rows:
        return "У вас пока нет запросов на возврат."
    lines = ["Последние возвраты:"]
    for row in rows:
        label = _STATUS_LABELS.get(row.status, row.status)
        lines.append(
            f"• {_money(row.amount_minor, row.currency)} · {row.credit_units} кр. · {label}"
        )
    return "\n".join(lines)


@router.message(Command("refund"))
async def refund_menu(
    message: Message,
    onboarding: OnboardingService,
    refunds: RefundService | None,
) -> None:
    user = await onboarding.current_user(message.from_user.id)
    if user is None or refunds is None:
        await message.answer("Возвраты сейчас недоступны.")
        return
    purchases = await refunds.eligible_purchases(user.id)
    if not purchases:
        await message.answer(
            "Сейчас нет покупок, подходящих для автоматического возврата. "
            "Для возврата нужны неиспользованные кредиты и покупка в пределах срока политики."
        )
        return
    await message.answer(
        "Выберите покупку. После подтверждения соответствующие кредиты будут "
        "зарезервированы до окончательного ответа платёжной системы.\n\n"
        "Важно: возврат платежа за подписку не отключает будущие продления. "
        "Автопродление управляется отдельно в разделе подписки.",
        reply_markup=_purchase_keyboard(purchases),
    )


@router.message(Command("refund_status"))
async def refund_status_command(
    message: Message,
    onboarding: OnboardingService,
    refunds: RefundService | None,
) -> None:
    user = await onboarding.current_user(message.from_user.id)
    if user is None or refunds is None:
        await message.answer("История возвратов сейчас недоступна.")
        return
    await message.answer(_history_text(await refunds.history(user.id)))


@router.callback_query(F.data == "refund:history")
async def refund_history_callback(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    refunds: RefundService | None,
) -> None:
    await callback.answer()
    user = await onboarding.current_user(callback.from_user.id)
    if user is None or refunds is None or not isinstance(callback.message, Message):
        return
    await callback.message.answer(_history_text(await refunds.history(user.id)))


@router.callback_query(F.data.startswith("refund:request:"))
async def request_refund_callback(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    refunds: RefundService | None,
) -> None:
    user = await onboarding.current_user(callback.from_user.id)
    parts = (callback.data or "").split(":")
    try:
        order_id = UUID(parts[2])
        credit_units = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    if user is None or refunds is None:
        await callback.answer("Возвраты сейчас недоступны.", show_alert=True)
        return
    result = await refunds.request_refund(user.id, order_id, credit_units)
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if result.outcome is RefundRequestOutcome.CREATED and result.refund is not None:
        await callback.message.answer(
            "Запрос принят. "
            f"Зарезервировано {result.refund.credit_units} кредитов; "
            f"сумма возврата — {_money(result.refund.amount_minor, result.refund.currency)}. "
            "Статус можно проверить командой /refund_status."
        )
        return
    messages = {
        RefundRequestOutcome.DISABLED: "Возвраты временно отключены.",
        RefundRequestOutcome.NOT_FOUND: "Пользователь или покупка не найдены.",
        RefundRequestOutcome.NOT_ELIGIBLE: (
            "Эта покупка не подходит для автоматического возврата."
        ),
        RefundRequestOutcome.INVALID_UNITS: (
            "Количество кредитов для возврата изменилось. Откройте /refund заново."
        ),
        RefundRequestOutcome.INSUFFICIENT_CREDITS: (
            "Часть кредитов уже использована, поэтому автоматический возврат невозможен."
        ),
        RefundRequestOutcome.PARTIAL_UNSUPPORTED: (
            "Для этой покупки доступен только полный возврат."
        ),
        RefundRequestOutcome.ALREADY_PENDING: (
            "По этой покупке уже есть незавершённый запрос на возврат."
        ),
    }
    await callback.message.answer(messages.get(result.outcome, "Возврат не удалось создать."))
