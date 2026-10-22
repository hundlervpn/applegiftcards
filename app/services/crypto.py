"""Симметричное шифрование кодов gift cards (Fernet / AES-128-CBC + HMAC-SHA256).

См. docs/sdd-scrum-18-gift-card-encryption.md
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class GiftCardCryptoError(Exception):
    """Базовая ошибка шифрования/расшифровки кода карты."""


class GiftCardEncryptionKeyError(GiftCardCryptoError):
    """FERNET_KEY отсутствует или невалиден."""


class GiftCardDecryptionError(GiftCardCryptoError):
    """Ciphertext повреждён, подделан или зашифрован другим ключом."""


def _get_fernet() -> Fernet:
    raw = get_settings().fernet_key.get_secret_value()
    if not raw:
        raise GiftCardEncryptionKeyError("FERNET_KEY is not configured")
    try:
        return Fernet(raw.encode("ascii"))
    except Exception as exc:
        raise GiftCardEncryptionKeyError(
            "FERNET_KEY is not a valid Fernet key"
        ) from exc


def encrypt_code(plaintext: str) -> bytes:
    """Шифрует код карты → bytes для ``gift_cards.code_encrypted``."""
    if not isinstance(plaintext, str) or plaintext == "":
        raise ValueError("gift card code must be a non-empty string")
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_code(token: bytes) -> str:
    """Расшифровывает ``code_encrypted`` → plaintext-код карты."""
    if not isinstance(token, (bytes, bytearray)) or not token:
        raise GiftCardDecryptionError("gift card ciphertext must be non-empty bytes")
    try:
        return _get_fernet().decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as exc:
        raise GiftCardDecryptionError("gift card code could not be decrypted") from exc
    except UnicodeDecodeError as exc:
        raise GiftCardDecryptionError(
            "decrypted gift card code is not valid UTF-8"
        ) from exc
