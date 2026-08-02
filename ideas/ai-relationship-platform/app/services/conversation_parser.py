"""Deterministic, provider-independent parser for pasted conversations."""

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum


class RejectionReason(StrEnum):
    EMPTY_CONTENT = "empty_content"
    TOO_SHORT = "too_short"
    ONE_PARTICIPANT = "one_participant"
    TOO_MANY_PARTICIPANTS = "too_many_participants"
    OVERSIZED = "oversized"
    UNSUPPORTED_FORMAT = "unsupported_format"
    NO_VALID_MESSAGES = "no_valid_messages"


class ConversationRejected(ValueError):
    def __init__(self, reason: RejectionReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class NormalizedMessage:
    id: str
    speaker: str
    timestamp: str | None
    text: str
    source_order: int


@dataclass(frozen=True)
class ParsedConversation:
    participants: dict[str, str]
    messages: list[NormalizedMessage]
    message_count: int
    character_count: int
    source_format: str

    def message_dicts(self) -> list[dict[str, object]]:
        return [asdict(message) for message in self.messages]


_PREFIX = re.compile(r"^(?:\[(?P<ts>[^]]+)\]\s*)?(?P<name>[^:\n]{1,100}):\s*(?P<text>.*)$")
_TELEGRAM = re.compile(r"^(?P<name>.+?),\s*\[(?P<ts>[^]]+)\]\s*$")
_TIMESTAMP_FORMATS = ("%d.%m.%Y %H:%M", "%H:%M")


def _timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    for timestamp_format in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value.strip(), timestamp_format).isoformat()
        except ValueError:
            continue
    return None


class ConversationParser:
    def __init__(
        self, min_messages: int = 4, max_characters: int = 30_000, max_participants: int = 2
    ) -> None:
        self.min_messages = min_messages
        self.max_characters = max_characters
        self.max_participants = max_participants

    def parse(self, content: str) -> ParsedConversation:
        if not content.strip():
            raise ConversationRejected(RejectionReason.EMPTY_CONTENT)
        if len(content) > self.max_characters:
            raise ConversationRejected(RejectionReason.OVERSIZED)
        raw: list[tuple[str, str | None, str]] = []
        current: tuple[str, str | None, list[str]] | None = None
        source_format = "simple_prefix"
        for line in content.strip().splitlines():
            telegram = _TELEGRAM.match(line.strip())
            prefix = _PREFIX.match(line.strip())
            if telegram:
                if current is not None:
                    raw.append((current[0], current[1], "\n".join(current[2]).strip()))
                current = (telegram.group("name").strip(), _timestamp(telegram.group("ts")), [])
                source_format = "telegram_multiline"
            elif prefix and source_format != "telegram_multiline":
                if current is not None:
                    raw.append((current[0], current[1], "\n".join(current[2]).strip()))
                current = (
                    prefix.group("name").strip(),
                    _timestamp(prefix.group("ts")),
                    [prefix.group("text")],
                )
                if prefix.group("ts"):
                    source_format = "timestamped_prefix"
            elif current is not None:
                current[2].append(line.rstrip())
        if current is not None:
            raw.append((current[0], current[1], "\n".join(current[2]).strip()))
        raw = [item for item in raw if item[2]]
        if not raw:
            reason = (
                RejectionReason.UNSUPPORTED_FORMAT
                if content.strip()
                else RejectionReason.NO_VALID_MESSAGES
            )
            raise ConversationRejected(reason)
        names = list(dict.fromkeys(item[0] for item in raw))
        if len(names) == 1:
            raise ConversationRejected(RejectionReason.ONE_PARTICIPANT)
        if len(names) > self.max_participants:
            raise ConversationRejected(RejectionReason.TOO_MANY_PARTICIPANTS)
        if len(raw) < self.min_messages:
            raise ConversationRejected(RejectionReason.TOO_SHORT)
        labels = {name: chr(65 + index) for index, name in enumerate(names)}
        messages = [
            NormalizedMessage(f"m{index}", labels[name], timestamp, text, index)
            for index, (name, timestamp, text) in enumerate(raw, 1)
        ]
        return ParsedConversation(
            {label: name for name, label in labels.items()},
            messages,
            len(messages),
            sum(len(message.text) for message in messages),
            source_format,
        )
