import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.config import Settings
from app.services import crypto
from app.services.crypto import (
    GiftCardDecryptionError,
    GiftCardEncryptionKeyError,
    decrypt_code,
    encrypt_code,
)


def _settings_with_key(key: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1:5432/test",
        fernet_key=key,
    )


class GiftCardCryptoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Fernet.generate_key().decode("ascii")
        self.settings = _settings_with_key(self.key)
        self.settings_patch = patch.object(
            crypto,
            "get_settings",
            return_value=self.settings,
        )
        self.settings_patch.start()

    def tearDown(self) -> None:
        self.settings_patch.stop()

    def test_round_trip(self) -> None:
        plaintext = "TEST-10USD-0001"
        token = encrypt_code(plaintext)
        self.assertIsInstance(token, bytes)
        self.assertNotEqual(token, plaintext.encode("utf-8"))
        self.assertEqual(decrypt_code(token), plaintext)

    def test_same_plaintext_yields_different_ciphertext(self) -> None:
        plaintext = "TEST-10USD-0001"
        first = encrypt_code(plaintext)
        second = encrypt_code(plaintext)
        self.assertNotEqual(first, second)
        self.assertEqual(decrypt_code(first), plaintext)
        self.assertEqual(decrypt_code(second), plaintext)

    def test_empty_plaintext_raises(self) -> None:
        with self.assertRaises(ValueError):
            encrypt_code("")

    def test_empty_key_raises(self) -> None:
        with patch.object(
            crypto,
            "get_settings",
            return_value=_settings_with_key(""),
        ):
            with self.assertRaises(GiftCardEncryptionKeyError):
                encrypt_code("TEST-10USD-0001")

    def test_invalid_key_raises(self) -> None:
        with patch.object(
            crypto,
            "get_settings",
            return_value=_settings_with_key("not-a-fernet-key"),
        ):
            with self.assertRaises(GiftCardEncryptionKeyError):
                encrypt_code("TEST-10USD-0001")

    def test_wrong_key_raises_decryption_error(self) -> None:
        token = encrypt_code("TEST-10USD-0001")
        other_key = Fernet.generate_key().decode("ascii")
        with patch.object(
            crypto,
            "get_settings",
            return_value=_settings_with_key(other_key),
        ):
            with self.assertRaises(GiftCardDecryptionError):
                decrypt_code(token)

    def test_corrupt_token_raises_decryption_error(self) -> None:
        with self.assertRaises(GiftCardDecryptionError):
            decrypt_code(b"not-a-fernet-token")

    def test_empty_token_raises_decryption_error(self) -> None:
        with self.assertRaises(GiftCardDecryptionError):
            decrypt_code(b"")


if __name__ == "__main__":
    unittest.main()
