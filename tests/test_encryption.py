import os

import pytest
from cryptography.exceptions import InvalidTag

from sm_common.encryption import (
    FieldEncryptor,
    decrypt_field,
    encrypt_field,
)


class TestFieldEncryption:
    def setup_method(self):
        self.key = os.urandom(32)

    def test_round_trip(self):
        plaintext = "Rahul Sharma"
        encrypted = encrypt_field(plaintext, self.key)
        assert decrypt_field(encrypted, self.key) == plaintext

    def test_version_prefix(self):
        encrypted = encrypt_field("test", self.key)
        assert encrypted.startswith("v1:")

    def test_different_ciphertext_each_time(self):
        e1 = encrypt_field("test", self.key)
        e2 = encrypt_field("test", self.key)
        assert e1 != e2  # Random nonce makes each encryption unique

    def test_wrong_key_fails(self):
        encrypted = encrypt_field("test", self.key)
        wrong_key = os.urandom(32)
        with pytest.raises(InvalidTag):
            decrypt_field(encrypted, wrong_key)

    def test_unknown_version_raises(self):
        with pytest.raises(ValueError, match="Unknown encryption version"):
            decrypt_field("v2:somedata", self.key)

    def test_unicode_round_trip(self):
        plaintext = "राहुल शर्मा"
        encrypted = encrypt_field(plaintext, self.key)
        assert decrypt_field(encrypted, self.key) == plaintext

    def test_empty_string_round_trip(self):
        encrypted = encrypt_field("", self.key)
        assert decrypt_field(encrypted, self.key) == ""


class TestFieldEncryptor:
    MASTER_KEY_HEX = os.urandom(32).hex()
    HASH_SALT = "test-salt-abc"

    def setup_method(self):
        self.enc = FieldEncryptor(self.MASTER_KEY_HEX, self.HASH_SALT)

    def test_round_trip(self):
        ct = self.enc.encrypt("Rahul Sharma")
        assert self.enc.decrypt(ct) == "Rahul Sharma"

    def test_format_prefix(self):
        ct = self.enc.encrypt("test")
        assert ct.startswith("v2:")
        parts = ct.split(":")
        assert len(parts) == 3

    def test_unique_ciphertext_per_call(self):
        assert self.enc.encrypt("same") != self.enc.encrypt("same")

    def test_unicode_round_trip(self):
        plaintext = "राहुल शर्मा"
        assert self.enc.decrypt(self.enc.encrypt(plaintext)) == plaintext

    def test_empty_string_round_trip(self):
        assert self.enc.decrypt(self.enc.encrypt("")) == ""

    def test_invalid_key_length_raises(self):
        with pytest.raises(ValueError, match="256 bits"):
            FieldEncryptor("deadbeef", self.HASH_SALT)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid ciphertext format"):
            self.enc.decrypt("garbage")

    def test_needs_reencrypt_current_version(self):
        ct = self.enc.encrypt("data")
        assert self.enc.needs_reencrypt(ct) is False

    def test_needs_reencrypt_old_version(self):
        enc_v3 = FieldEncryptor(self.MASTER_KEY_HEX, self.HASH_SALT, key_version=3)
        ct_v2 = self.enc.encrypt("data")  # v2: prefix
        assert enc_v3.needs_reencrypt(ct_v2) is True

    def test_reencrypt_round_trip(self):
        enc_v3 = FieldEncryptor(self.MASTER_KEY_HEX, self.HASH_SALT, key_version=3)
        ct_v2 = self.enc.encrypt("hello")
        # reencrypt with v3 encryptor — must decrypt with same key
        ct_v3 = enc_v3.reencrypt(ct_v2)
        assert ct_v3.startswith("v3:")
        assert enc_v3.decrypt(ct_v3) == "hello"

    def test_hash_for_lookup_deterministic(self):
        h1 = self.enc.hash_for_lookup("  Test@example.com  ")
        h2 = self.enc.hash_for_lookup("  test@example.com  ")
        assert h1 == h2
        assert len(h1) == 64  # hex SHA-256

    def test_legacy_v1_decrypt_raises_without_legacy_key(self):
        with pytest.raises(ValueError, match="no legacy_master_key"):
            self.enc.decrypt("v1:somefakeciphertext")

    def test_legacy_v1_decrypt_with_legacy_key(self):
        legacy_secret = "my-old-fernet-secret"
        enc_with_legacy = FieldEncryptor(
            self.MASTER_KEY_HEX, self.HASH_SALT, legacy_master_key=legacy_secret
        )
        # Produce a real v1: Fernet token using the same PBKDF2 derivation
        import base64

        from cryptography.fernet import Fernet as _Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"queuecare-v1",
            iterations=480_000,
        )
        fernet_key = base64.urlsafe_b64encode(kdf.derive(legacy_secret.encode()))
        token = _Fernet(fernet_key).encrypt(b"original plaintext").decode()
        legacy_ct = f"v1:{token}"
        assert enc_with_legacy.decrypt(legacy_ct) == "original plaintext"
        assert enc_with_legacy.needs_reencrypt(legacy_ct) is True
