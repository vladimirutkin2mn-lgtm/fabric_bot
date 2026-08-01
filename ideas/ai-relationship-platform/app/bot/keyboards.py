"""Inline keyboard factories."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts


def age_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Мне есть 18", callback_data="onboarding:age:yes"),
                InlineKeyboardButton(text="Мне нет 18", callback_data="onboarding:age:no"),
            ]
        ]
    )


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Принять условия", callback_data="onboarding:consent")]
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.ANALYZE, callback_data="menu:analyze")],
            [InlineKeyboardButton(text=texts.HISTORY, callback_data="menu:history")],
            [InlineKeyboardButton(text=texts.BALANCE, callback_data="menu:balance")],
            [InlineKeyboardButton(text=texts.PRIVACY, callback_data="menu:privacy")],
        ]
    )
