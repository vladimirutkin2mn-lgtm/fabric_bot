"""Inline keyboard factories."""
# ruff: noqa: RUF001

from uuid import UUID

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


def exit_rows(analysis_id: UUID, *, resend: bool = False) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if resend:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отправить заново", callback_data=f"intake:reset:{analysis_id}"
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="Отменить", callback_data=f"intake:cancel:{analysis_id}")],
            [
                InlineKeyboardButton(
                    text="Вернуться в меню", callback_data=f"intake:menu:{analysis_id}"
                )
            ],
        ]
    )
    return rows


def cancel_keyboard(analysis_id: UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=exit_rows(analysis_id))


def participant_keyboard(analysis_id: UUID, participants: dict[str, str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"intake:participant:{analysis_id}:{label}")]
        for label, name in participants.items()
    ]
    rows.extend(exit_rows(analysis_id, resend=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def goal_keyboard(analysis_id: UUID) -> InlineKeyboardMarkup:
    options = [
        "Есть ли у человека интерес?",
        "Общение стало холоднее?",
        "Стоит ли написать сейчас?",
        "Как лучше ответить?",
        "Что изменилось?",
    ]
    rows = [
        [InlineKeyboardButton(text=value, callback_data=f"intake:goal:{analysis_id}:{index}")]
        for index, value in enumerate(options)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="Свой вопрос", callback_data=f"intake:goal:{analysis_id}:custom"
            )
        ]
    )
    rows.extend(exit_rows(analysis_id, resend=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stage_keyboard(analysis_id: UUID) -> InlineKeyboardMarkup:
    options = [
        ("Только познакомились", "new_connection"),
        ("Ходим на свидания", "dating"),
        ("В отношениях", "relationship"),
        ("После расставания", "post_breakup"),
        ("Сложно определить", "unclear"),
        ("Пропустить", "not_provided"),
    ]
    rows = [
        [InlineKeyboardButton(text=text, callback_data=f"intake:stage:{analysis_id}:{code}")]
        for text, code in options
    ]
    rows.extend(exit_rows(analysis_id, resend=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)
