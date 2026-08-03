"""Inline keyboard factories."""
# ruff: noqa: RUF001

from collections.abc import Sequence
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.domain.products import ProductCatalog


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


def report_actions_keyboard(analysis_id: object) -> InlineKeyboardMarkup:
    value = str(analysis_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Варианты ответа", callback_data=f"report:replies:{value}")],
            [
                InlineKeyboardButton(
                    text="Задать уточняющий вопрос", callback_data=f"report:followup:{value}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Разобрать новый фрагмент", callback_data=f"report:new_fragment:{value}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Удалить разбор", callback_data=f"report:delete_prompt:{value}"
                )
            ],
            [InlineKeyboardButton(text="Вернуться в меню", callback_data="report:menu")],
        ]
    )


def feedback_keyboard(analysis_id: object) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=str(score), callback_data=f"feedback:{analysis_id}:{score}"
                )
                for score in range(1, 6)
            ]
        ]
    )


def deletion_keyboard(analysis_id: object) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Удалить", callback_data=f"report:delete_confirm:{analysis_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена", callback_data=f"report:delete_cancel:{analysis_id}"
                )
            ],
        ]
    )


def corrupted_report_keyboard(analysis_id: object) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Удалить разбор",
                    callback_data=f"report:delete_prompt:{analysis_id}",
                )
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="report:menu")],
        ]
    )


def history_keyboard(
    items: Sequence[tuple[object, str]], page: int, has_next: bool
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"history:open:{item_id}")]
        for item_id, label in items
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="← Назад", callback_data=f"history:page:{page - 1}")
        )
    if has_next:
        navigation.append(
            InlineKeyboardButton(text="Вперёд →", callback_data=f"history:page:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="report:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def billing_keyboard(
    analysis_id: UUID, price: int, preview_available: bool
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if preview_available:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Посмотреть бесплатное превью",
                    callback_data=f"billing:preview:{analysis_id}",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=f"Получить полный отчёт за {price} кредитов",
                    callback_data=f"billing:full:{analysis_id}",
                )
            ],
            [InlineKeyboardButton(text="Купить кредиты", callback_data="menu:balance")],
            [InlineKeyboardButton(text="Вернуться в меню", callback_data="report:menu")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def products_keyboard(catalog: ProductCatalog) -> InlineKeyboardMarkup:
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


def checkout_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть тестовую оплату", url=url)],
            [InlineKeyboardButton(text="Обновить баланс", callback_data="credits:refresh")],
        ]
    )


def preview_actions_keyboard(analysis_id: UUID, price: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Открыть полный отчёт за {price} кредитов",
                    callback_data=f"billing:unlock:{analysis_id}",
                )
            ],
            [InlineKeyboardButton(text="Купить кредиты", callback_data="menu:balance")],
            [
                InlineKeyboardButton(
                    text="Удалить разбор", callback_data=f"report:delete_prompt:{analysis_id}"
                )
            ],
            [InlineKeyboardButton(text="Вернуться в меню", callback_data="report:menu")],
        ]
    )
