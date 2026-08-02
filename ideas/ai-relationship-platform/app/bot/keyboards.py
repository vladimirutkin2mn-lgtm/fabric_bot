"""Inline keyboard factories."""
# ruff: noqa: RUF001

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


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="intake:cancel")]]
    )


def participant_keyboard(participants: dict[str, str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"intake:participant:{label}")]
        for label, name in participants.items()
    ]
    rows.append([InlineKeyboardButton(text="Отправить заново", callback_data="intake:resend")])
    rows.append([InlineKeyboardButton(text="Отменить", callback_data="intake:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def goal_keyboard() -> InlineKeyboardMarkup:
    options = [
        "Есть ли у человека интерес?",
        "Общение стало холоднее?",
        "Стоит ли написать сейчас?",
        "Как лучше ответить?",
        "Что изменилось?",
    ]
    rows = [
        [InlineKeyboardButton(text=value, callback_data=f"intake:goal:{index}")]
        for index, value in enumerate(options)
    ]
    rows.append([InlineKeyboardButton(text="Свой вопрос", callback_data="intake:goal:custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stage_keyboard() -> InlineKeyboardMarkup:
    options = [
        ("Только познакомились", "new_connection"),
        ("Ходим на свидания", "dating"),
        ("В отношениях", "relationship"),
        ("После расставания", "post_breakup"),
        ("Сложно определить", "unclear"),
        ("Пропустить", "not_provided"),
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=f"intake:stage:{code}")]
            for text, code in options
        ]
    )
