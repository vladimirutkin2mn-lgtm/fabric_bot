import pytest

from app.services.sensitive_content import (
    AESGCMSensitiveContentCipher,
    ContentAuthenticationError,
    ContentPurpose,
)


def test_encryption_is_random_and_authenticated() -> None:
    cipher = AESGCMSensitiveContentCipher("a sufficiently long unit test key material")
    value = {"private": "sentinel", "number": 1}
    first = cipher.encrypt_json(ContentPurpose.ANALYSIS_SOURCE, value)
    second = cipher.encrypt_json(ContentPurpose.ANALYSIS_SOURCE, value)
    assert first != second
    assert cipher.decrypt_json(ContentPurpose.ANALYSIS_SOURCE, first) == value
    assert b"sentinel" not in first
    with pytest.raises(ContentAuthenticationError):
        cipher.decrypt_json(ContentPurpose.ANALYSIS_RESULT, first)
    with pytest.raises(ContentAuthenticationError):
        AESGCMSensitiveContentCipher("different key").decrypt_json(
            ContentPurpose.ANALYSIS_SOURCE, first
        )


def test_keyed_fingerprints_are_deterministic_and_domain_separated() -> None:
    value = {"private": "short guessable text", "number": 1}
    cipher = AESGCMSensitiveContentCipher("fingerprint key material")
    first = cipher.fingerprint_json(ContentPurpose.TELEGRAM_UPDATE, value)
    second = cipher.fingerprint_json(ContentPurpose.TELEGRAM_UPDATE, value)
    assert first == second
    assert first != cipher.fingerprint_json(ContentPurpose.ANALYSIS_SOURCE, value)
    assert first != AESGCMSensitiveContentCipher("different key").fingerprint_json(
        ContentPurpose.TELEGRAM_UPDATE, value
    )
    assert "short guessable text" not in first


def test_tampering_and_repr_do_not_disclose_private_values() -> None:
    secret = "unique-secret-key-sentinel"
    cipher = AESGCMSensitiveContentCipher(secret)
    encrypted = cipher.encrypt_json(ContentPurpose.ANALYSIS_SOURCE, "private-sentinel")
    with pytest.raises(ContentAuthenticationError) as caught:
        cipher.decrypt_json(
            ContentPurpose.ANALYSIS_SOURCE, encrypted[:-1] + bytes((encrypted[-1] ^ 1,))
        )
    assert secret not in repr(cipher)
    assert secret not in str(caught.value)
    assert "private-sentinel" not in str(caught.value)
