"""Conversation parser behavior and privacy-safe validation."""
# ruff: noqa: RUF001, E501

import pytest

from app.services.conversation_parser import (
    ConversationParser,
    ConversationRejected,
    RejectionReason,
)


def test_simple_prefix_is_deterministic_and_preserves_text() -> None:
    text = "Анна: Привет! 😊\nИван: Привет: как ты?\nАнна: Хорошо\nИван: Отлично"
    parser = ConversationParser()
    first = parser.parse(text)
    assert first == parser.parse(text)
    assert [message.id for message in first.messages] == ["m1", "m2", "m3", "m4"]
    assert first.messages[1].text == "Привет: как ты?"
    assert first.participants == {"A": "Анна", "B": "Иван"}


def test_timestamp_and_multiline_formats() -> None:
    timestamped = ConversationParser(min_messages=2).parse(
        "[12.07.2026 18:45] Анна: Привет!\n[18:47] Иван: Привет"
    )
    assert timestamped.messages[0].timestamp == "2026-07-12T18:45:00"
    assert timestamped.messages[1].timestamp == "18:47:00"
    multiline = ConversationParser(min_messages=2).parse(
        "Анна, [12.07.2026 18:45]\nПривет!\nКак день?\n\nИван, [12.07.2026 18:47]\nПривет\nНормально"
    )
    assert multiline.messages[0].text == "Привет!\nКак день?"
    assert multiline.source_format == "telegram_multiline"


def test_multiline_colon_continuation_blank_lines_and_order_are_preserved() -> None:
    parsed = ConversationParser(min_messages=2).parse(
        "Анна, [bad timestamp]\nПривет 😊\nПричина: хочу обсудить завтра\n\n"
        "Иван, [12.07.2026 18:47]\nХорошо: давай"
    )
    assert parsed.participants == {"A": "Анна", "B": "Иван"}
    assert parsed.messages[0].timestamp is None
    assert parsed.messages[0].text == "Привет 😊\nПричина: хочу обсудить завтра"
    assert parsed.messages[1].text == "Хорошо: давай"
    assert [message.source_order for message in parsed.messages] == [1, 2]


def test_duplicate_looking_names_remain_distinct() -> None:
    parsed = ConversationParser().parse("Анна: 1\nАнна.: 2\nАнна: 3\nАнна.: 4")
    assert parsed.participants == {"A": "Анна", "B": "Анна."}


@pytest.mark.parametrize(
    "content,reason",
    [
        ("", RejectionReason.EMPTY_CONTENT),
        ("  \n", RejectionReason.EMPTY_CONTENT),
        ("обычный текст", RejectionReason.UNSUPPORTED_FORMAT),
        ("А: 1\nА: 2\nА: 3\nА: 4", RejectionReason.ONE_PARTICIPANT),
        ("А: 1\nБ: 2", RejectionReason.TOO_SHORT),
        ("А: 1\nБ: 2\nВ: 3\nА: 4", RejectionReason.TOO_MANY_PARTICIPANTS),
    ],
)
def test_rejections(content: str, reason: RejectionReason) -> None:
    with pytest.raises(ConversationRejected) as caught:
        ConversationParser().parse(content)
    assert caught.value.reason is reason


def test_oversized_is_rejected_without_content_in_exception() -> None:
    secret = "СЕКРЕТНЫЙ ТЕКСТ"
    with pytest.raises(ConversationRejected) as caught:
        ConversationParser(max_characters=3).parse(secret)
    assert caught.value.reason is RejectionReason.OVERSIZED
    assert secret not in str(caught.value)
