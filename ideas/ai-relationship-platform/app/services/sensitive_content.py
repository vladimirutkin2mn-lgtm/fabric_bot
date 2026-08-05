"""Versioned authenticated encryption for private JSON content."""

import base64
import hashlib
import hmac
import json
import os
from enum import StrEnum
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class ContentPurpose(StrEnum):
    ANALYSIS_SOURCE = "analysis-source"
    ANALYSIS_RESULT = "analysis-result"
    TELEGRAM_UPDATE = "telegram-update"
    TELEGRAM_FSM_DATA = "telegram-fsm-data"
    PAYMENT_METHOD = "payment-method"
    FOLLOW_UP_QUESTION = "follow-up-question"
    FOLLOW_UP_ANSWER = "follow-up-answer"


class SensitiveContentError(ValueError):
    """Safe base error: its message intentionally contains no sensitive values."""


class MalformedEnvelopeError(SensitiveContentError):
    pass


class UnknownEnvelopeVersionError(SensitiveContentError):
    pass


class ContentAuthenticationError(SensitiveContentError):
    pass


class SensitiveContentCipher(Protocol):
    def encrypt_json(self, purpose: ContentPurpose, value: object) -> bytes: ...
    def decrypt_json(self, purpose: ContentPurpose, value: bytes) -> object: ...


class FingerprintingSensitiveContentCipher(SensitiveContentCipher, Protocol):
    def fingerprint_json(self, purpose: ContentPurpose, value: object) -> str: ...


class AESGCMSensitiveContentCipher:
    """AES-256-GCM with HKDF purpose separation and random 96-bit nonces."""

    _MAGIC = b"HS"
    _VERSION = 1

    def __init__(self, root_key: str | bytes) -> None:
        material = root_key.encode() if isinstance(root_key, str) else root_key
        if not material:
            raise ValueError("content encryption key is required")
        self._root = hashlib.sha256(material).digest()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    @staticmethod
    def _canonical_json(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    def _key(self, purpose: ContentPurpose) -> bytes:
        derived: bytes = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"HeartSignal/content/v1",
            info=purpose.value.encode(),
        ).derive(self._root)
        return derived

    def _fingerprint_key(self, purpose: ContentPurpose) -> bytes:
        derived: bytes = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"HeartSignal/fingerprint/v1",
            info=purpose.value.encode(),
        ).derive(self._root)
        return derived

    def encrypt_json(self, purpose: ContentPurpose, value: object) -> bytes:
        plaintext = self._canonical_json(value)
        nonce = os.urandom(12)
        header = self._MAGIC + bytes((self._VERSION,))
        ciphertext: bytes = AESGCM(self._key(purpose)).encrypt(
            nonce, plaintext, header + purpose.value.encode()
        )
        return header + nonce + ciphertext

    def decrypt_json(self, purpose: ContentPurpose, value: bytes) -> object:
        if not isinstance(value, bytes) or len(value) < 32 or value[:2] != self._MAGIC:
            raise MalformedEnvelopeError("malformed sensitive-content envelope")
        if value[2] != self._VERSION:
            raise UnknownEnvelopeVersionError("unsupported sensitive-content envelope version")
        header, nonce, ciphertext = value[:3], value[3:15], value[15:]
        try:
            plaintext = AESGCM(self._key(purpose)).decrypt(
                nonce, ciphertext, header + purpose.value.encode()
            )
            return json.loads(plaintext)
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentAuthenticationError("sensitive content authentication failed") from exc

    def fingerprint_json(self, purpose: ContentPurpose, value: object) -> str:
        """Return a deterministic keyed digest safe from offline plaintext guessing."""
        return hmac.new(
            self._fingerprint_key(purpose),
            self._canonical_json(value),
            hashlib.sha256,
        ).hexdigest()


def decode_configured_key(value: str) -> bytes:
    """Accept opaque text and explicitly prefixed base64 without leaking parse input."""
    if value.startswith("base64:"):
        try:
            return base64.b64decode(value[7:], validate=True)
        except ValueError as exc:
            raise ValueError("invalid encoded content encryption key") from exc
    return value.encode()
