import os

import pytest
from cryptography.fernet import Fernet

from sm_common.encryption import (
    decrypt_field,
    decrypt_from_transport,
    encrypt_field,
    encrypt_for_transport,
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
        with pytest.raises(Exception):
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


class TestTransportEncryption:
    def setup_method(self):
        self.transport_key = Fernet.generate_key().decode()

    def test_round_trip(self):
        plaintext = "+91 98765 43210"
        encrypted = encrypt_for_transport(plaintext, self.transport_key)
        assert decrypt_from_transport(encrypted, self.transport_key) == plaintext

    def test_wrong_key_fails(self):
        encrypted = encrypt_for_transport("test", self.transport_key)
        wrong_key = Fernet.generate_key().decode()
        with pytest.raises(Exception):
            decrypt_from_transport(encrypted, wrong_key)

    def test_unicode_round_trip(self):
        plaintext = "राहुल शर्मा"
        encrypted = encrypt_for_transport(plaintext, self.transport_key)
        assert decrypt_from_transport(encrypted, self.transport_key) == plaintext
