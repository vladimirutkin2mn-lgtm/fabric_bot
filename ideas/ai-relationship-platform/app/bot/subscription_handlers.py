"""Telegram subscription checkout and lifecycle management."""
# ruff: noqa: RUF001

from uuid import UUID

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.db.models import User
from app.domain.products import ProductCatalog
from app.services.credits_service import CreditsService
from app.services.onboarding import OnboardingService
from app.services.subscription_checkout_service import (
    SubscriptionCheckoutRejected,
    SubscriptionCheckoutService,
)
from app.services.subscription_management_service import (
    SubscriptionManagementOutcome,
    SubscriptionManagementService,
    SubscriptionView,
)

router = Router(name="subscriptions")


def subscription_market_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="International · EUR",
                    callback_data="credits:offer:subscription_monthly:INTERNATIONAL:EUR",
                )
            ],
            [
                InlineKeyboardButton(
                    text="International · USD",
                    callback_data="credits:offer:subscription_monthly:INTERNATIONAL:USD",
                )
            ],
            [InlineKeyboardButton(text="Вернуться", callback_data="menu:balance")],
        ]
    )


def subscription_checkout_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть защищённую оплату", url=url)],
            [InlineKeyboardButton(text="Обновить статус", callback_data="subscription:refresh")],
        ]
    )


def subscription_management_keyboard(value: SubscriptionView) -> InlineKeyboardMarkup:
    action = (
        InlineKeyboardButton(
            text="Возобновить автопродление",
            callback_data=f"subscription:resume:{value.id}",
        )
        if value.status == "cancel_at_period_end"
        else InlineKeyboardButton(
            text="Отключить автопродление",
            callback_data=f"subscription:cancel:{value.id}",
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [action],
            [InlineKeyboardButton(text="Обновить статус", callback_data="subscription:refresh")],
            [InlineKeyboardButton(text="Вернуться в меню", callback_data="report:menu")],
        ]
    )


def _status_text(value: SubscriptionView) -> str:
    labels = {
        "incomplete": "ожидает первой оплаты",
        "active": "активна",
        "past_due": "платёж не прошёл",
        "cancel_at_period_end": "автопродление отключено",
        "paused": "приостановлена",
    }
    boundary = (
        value.current_period_end.strftime("%d.%m.%Y")
        if value.current_period_end is not None
        else "не определена"
    )
    suffix = (
        f"Доступ сохранится до {boundary}."
        if value.status == "cancel_at_period_end"
        else f"Следующая граница периода: {boundary}."
    )
    return f"Подписка: {labels.get(value.status, value.status)}.\n{suffix}"


async def _current_user_subscription(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    subscriptions: SubscriptionManagementService | None,
) -> tuple[User | None, SubscriptionView | None]:
    user = await onboarding.current_user(callback.from_user.id)
    current = (
        None if user is None or subscriptions is None else await subscriptions.current(user.id)
    )
    return user, current


@router.callback_query(F.data.in_({"menu:balance", "credits:refresh", "subscription:refresh"}))
async def balance_and_subscription_screen(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    credits: CreditsService,
    catalog: ProductCatalog,
    subscriptions: SubscriptionManagementService | None,
    billing_settings: Settings,
    analysis_price: int,
) -> None:
    await callback.answer()
    user, current = await _current_user_subscription(callback, onboarding, subscriptions)
    if user is None or not isinstance(callback.message, Message):
        return
    balance = await credits.balance(user.id)
    subscription_note = (
        "\n\n" + _status_text(current)
        if current is not None
        else (
            "\n\nМесячная подписка начисляет кредиты после каждого подтверждённого платежа. "
            "Автопродление можно отключить в любой момент."
            if billing_settings.subscriptions_enabled
            else ""
        )
    )
    await callback.message.answer(
        f"Ваш баланс: {balance} кредитов\n"
        f"Один полный разбор стоит: {analysis_price} кредитов"
        f"{subscription_note}",
        reply_markup=(
            subscription_management_keyboard(current)
            if current is not None
            else _products_keyboard(catalog)
        ),
    )


def _products_keyboard(catalog: ProductCatalog) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{product.title} — {product.credits} кр.",
                callback_data=f"credits:buy:{product.code.value}",
            )
        ]
        for product in catalog.all()
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="Обновить баланс", callback_data="credits:refresh")],
            [InlineKeyboardButton(text="Вернуться в меню", callback_data="report:menu")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "credits:buy:subscription_monthly")
async def choose_subscription_market(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    subscriptions: SubscriptionManagementService | None,
    billing_settings: Settings,
) -> None:
    await callback.answer()
    user, current = await _current_user_subscription(callback, onboarding, subscriptions)
    if user is None or not isinstance(callback.message, Message):
        return
    if current is not None:
        await callback.message.answer(
            _status_text(current), reply_markup=subscription_management_keyboard(current)
        )
        return
    if not billing_settings.subscriptions_enabled:
        await callback.message.answer("Подписка сейчас недоступна.")
        return
    await callback.message.answer(
        "Выберите валюту ежемесячной подписки. Сумма и период будут показаны Stripe до оплаты.",
        reply_markup=subscription_market_keyboard(),
    )


@router.callback_query(F.data.startswith("credits:offer:subscription_monthly:"))
async def create_subscription_checkout(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    subscription_checkout: SubscriptionCheckoutService | None,
) -> None:
    await callback.answer()
    user = await onboarding.current_user(callback.from_user.id)
    if user is None or subscription_checkout is None or not isinstance(callback.message, Message):
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await callback.message.answer("Этот вариант подписки недоступен.")
        return
    _, _, product_code, market, currency = parts
    try:
        result = await subscription_checkout.create_checkout(
            user.id, product_code, market, currency
        )
    except SubscriptionCheckoutRejected:
        await callback.message.answer("Подписка сейчас недоступна. Попробуйте позже.")
        return
    if result.url is None:
        await callback.message.answer("Подписка создаётся. Обновите статус через несколько секунд.")
        return
    await callback.message.answer(
        "Stripe покажет сумму, период и условия автопродления до подтверждения оплаты.",
        reply_markup=subscription_checkout_keyboard(result.url),
    )


@router.callback_query(F.data.startswith("subscription:cancel:"))
async def cancel_subscription(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    subscriptions: SubscriptionManagementService | None,
) -> None:
    await _change_subscription(callback, onboarding, subscriptions, resume=False)


@router.callback_query(F.data.startswith("subscription:resume:"))
async def resume_subscription(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    subscriptions: SubscriptionManagementService | None,
) -> None:
    await _change_subscription(callback, onboarding, subscriptions, resume=True)


async def _change_subscription(
    callback: CallbackQuery,
    onboarding: OnboardingService,
    subscriptions: SubscriptionManagementService | None,
    *,
    resume: bool,
) -> None:
    user = await onboarding.current_user(callback.from_user.id)
    try:
        subscription_id = UUID((callback.data or "").split(":")[-1])
    except ValueError:
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    if user is None or subscriptions is None:
        await callback.answer("Подписка недоступна.", show_alert=True)
        return
    outcome = (
        await subscriptions.resume(user.id, subscription_id)
        if resume
        else await subscriptions.cancel(user.id, subscription_id)
    )
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if outcome is SubscriptionManagementOutcome.UPDATED:
        current = await subscriptions.current(user.id)
        text = (
            "Автопродление возобновлено."
            if resume
            else "Автопродление отключено. Уже начисленные кредиты сохраняются."
        )
        await callback.message.answer(
            text,
            reply_markup=(
                subscription_management_keyboard(current) if current is not None else None
            ),
        )
    elif outcome is SubscriptionManagementOutcome.ALREADY_SET:
        await callback.message.answer("Состояние подписки уже актуально.")
    elif outcome is SubscriptionManagementOutcome.UNAVAILABLE:
        await callback.message.answer("Управление подпиской временно недоступно.")
    else:
        await callback.message.answer("Подписка не найдена.")
