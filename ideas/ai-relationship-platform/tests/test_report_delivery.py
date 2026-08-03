"""Telegram report delivery sequencing and failure tests."""

from typing import Any, cast

import pytest
from aiogram.types import Message

from app.bot import texts
from app.bot.report_delivery import deliver_report
from app.services.report_renderer import RenderedReport


class MessageRecorder:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_at = fail_at

    async def answer(self, text: str, **kwargs: Any) -> None:
        index = len(self.calls)
        if self.fail_at == index:
            raise RuntimeError("SECRET-PRIVATE-CONTENT")
        self.calls.append((text, kwargs.get("reply_markup")))


@pytest.mark.parametrize("chunks", [("one",), ("one", "two", "three")])
async def test_chunks_order_final_actions_and_separate_feedback(chunks: tuple[str, ...]) -> None:
    message = MessageRecorder()
    await deliver_report(cast(Message, message), "id", RenderedReport(chunks))
    assert [call[0] for call in message.calls[:-1]] == list(chunks)
    assert all(call[1] is None for call in message.calls[:-2])
    assert message.calls[-2][1] is not None
    assert message.calls[-1][0] == texts.FEEDBACK_PROMPT and message.calls[-1][1] is not None


async def test_existing_feedback_has_no_feedback_keyboard() -> None:
    message = MessageRecorder()
    await deliver_report(
        cast(Message, message), "id", RenderedReport(("report",)), feedback_exists=True
    )
    assert message.calls[-1] == (texts.FEEDBACK_ALREADY, None)


@pytest.mark.parametrize("fail_at", [0, 1, 2])
async def test_send_failure_is_propagated_without_private_logging(
    fail_at: int, caplog: pytest.LogCaptureFixture
) -> None:
    message = MessageRecorder(fail_at)
    with pytest.raises(RuntimeError):
        await deliver_report(cast(Message, message), "safe-id", RenderedReport(("one", "two")))
    assert "SECRET-PRIVATE-CONTENT" not in caplog.text
    assert "safe-id" in caplog.text


async def test_oversized_or_empty_chunks_are_rejected_before_telegram() -> None:
    for chunk in ("", "x" * 4097):
        with pytest.raises(ValueError, match="invalid_report_chunk"):
            await deliver_report(cast(Message, MessageRecorder()), "id", RenderedReport((chunk,)))
