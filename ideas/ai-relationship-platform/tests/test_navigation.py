"""Keyboard labels and callback contracts."""

from app.bot import texts
from app.bot.keyboards import main_menu_keyboard


def test_main_menu_contains_required_sections() -> None:
    keyboard = main_menu_keyboard()
    assert [row[0].text for row in keyboard.inline_keyboard] == [
        texts.ANALYZE,
        texts.HISTORY,
        texts.BALANCE,
        texts.PRIVACY,
    ]


def test_unimplemented_section_copy_is_exact() -> None:
    assert texts.COMING_LATER == "Раздел появится на следующем этапе."
