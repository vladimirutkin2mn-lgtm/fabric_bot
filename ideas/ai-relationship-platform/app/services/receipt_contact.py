"""Validation boundary for YooKassa receipt delivery contacts."""

import re
from dataclasses import dataclass

_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_PHONE = re.compile(r"^\+[1-9][0-9]{7,14}$")


class InvalidReceiptContact(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class ReceiptContact:
    value: str

    def __repr__(self) -> str:
        return "ReceiptContact(<redacted>)"


def validate_receipt_contact(value: str) -> ReceiptContact:
    candidate = value.strip()
    if not (_EMAIL.fullmatch(candidate) or _PHONE.fullmatch(candidate)):
        raise InvalidReceiptContact("invalid receipt contact")
    return ReceiptContact(candidate)
