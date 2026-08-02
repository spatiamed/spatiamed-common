import hashlib

import pytest

from sm_common.phone import (
    hash_normalized,
    hash_phone,
    hash_phone_e164,
    normalize_e164,
    normalize_phone,
)


class TestNormalizePhone:
    def test_ten_digit(self):
        assert normalize_phone("9876543210") == "9876543210"

    def test_plus91_prefix(self):
        assert normalize_phone("+919876543210") == "9876543210"

    def test_91_prefix(self):
        assert normalize_phone("919876543210") == "9876543210"

    def test_zero_prefix(self):
        assert normalize_phone("09876543210") == "9876543210"

    def test_spaces(self):
        assert normalize_phone("+91 98765 43210") == "9876543210"

    def test_dashes(self):
        assert normalize_phone("98765-43210") == "9876543210"

    def test_parens_and_spaces(self):
        assert normalize_phone("(+91) 98765 43210") == "9876543210"

    def test_leading_trailing_whitespace(self):
        assert normalize_phone("  9876543210  ") == "9876543210"

    def test_invalid_too_short(self):
        with pytest.raises(ValueError, match="Invalid Indian mobile number"):
            normalize_phone("12345")

    def test_invalid_starts_with_low_digit(self):
        with pytest.raises(ValueError, match="Invalid Indian mobile number"):
            normalize_phone("1234567890")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="Invalid Indian mobile number"):
            normalize_phone("")

    def test_all_valid_starting_digits(self):
        for d in "6789":
            assert normalize_phone(f"{d}000000000") == f"{d}000000000"


class TestHashPhone:
    def test_deterministic(self):
        h1 = hash_phone("9876543210", "salt")
        h2 = hash_phone("9876543210", "salt")
        assert h1 == h2

    def test_critical_contract(self):
        """hash_phone("+91 98765 43210", salt) == hash_phone("9876543210", salt)"""
        assert hash_phone("+91 98765 43210", "mysalt") == hash_phone("9876543210", "mysalt")

    def test_different_salt_different_hash(self):
        assert hash_phone("9876543210", "salt1") != hash_phone("9876543210", "salt2")

    def test_returns_64_char_hex(self):
        h = hash_phone("9876543210", "salt")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_invalid_phone_raises(self):
        with pytest.raises(ValueError):
            hash_phone("invalid", "salt")

    def test_delegates_to_hash_normalized(self):
        # hash_phone MUST produce the exact same bytes as the low-level primitive
        # over the normalized value — i.e. hash_phone is pure delegation.
        assert hash_phone("+91 98765 43210", "s") == hash_normalized("9876543210", "s")


class TestHashNormalized:
    def test_scheme_is_salt_then_value_sha256(self):
        assert (
            hash_normalized("9876543210", "salt") == hashlib.sha256(b"salt9876543210").hexdigest()
        )

    def test_deterministic(self):
        assert hash_normalized("x", "s") == hash_normalized("x", "s")

    def test_different_salt_different_hash(self):
        assert hash_normalized("x", "s1") != hash_normalized("x", "s2")


class TestNormalizeE164:
    def test_plus_prefix_stripped(self):
        assert normalize_e164("+1 415 555 0123") == "14155550123"

    def test_double_zero_prefix_stripped(self):
        assert normalize_e164("001 415 555 0123") == "14155550123"

    def test_keeps_country_code_digits(self):
        assert normalize_e164("+44 20 7946 0958") == "442079460958"

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="Invalid international number"):
            normalize_e164("+1 234")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Invalid international number"):
            normalize_e164("")


class TestHashPhoneE164:
    SAMPLE = "+1 415 555 0123"
    SALT = "testsalt"

    def test_byte_identical_to_careloop_scheme(self):
        """KAT: sm_common.hash_phone_e164 == CareLoop's hand-rolled scheme.

        CareLoop app/services/phone.py computes exactly:
            hashlib.sha256(f"{salt}{normalize_e164(phone)}".encode()).hexdigest()
        This proves delegation to sm_common is byte-for-byte identical, so CareLoop
        can drop its hand-rolled SHA-256 without changing any stored hash.
        """
        careloop_scheme = hashlib.sha256(
            f"{self.SALT}{normalize_e164(self.SAMPLE)}".encode()
        ).hexdigest()
        assert hash_phone_e164(self.SAMPLE, self.SALT) == careloop_scheme

    def test_pinned_known_answer(self):
        # Frozen digest — any drift in salt/algorithm/normalization breaks this.
        assert (
            hash_phone_e164(self.SAMPLE, self.SALT)
            == "8526b38f202cb729d154e287d0b5a6e112a5879e3217f658ba872a4b5d60d41c"
        )

    def test_delegates_to_hash_normalized(self):
        assert hash_phone_e164(self.SAMPLE, self.SALT) == hash_normalized(
            normalize_e164(self.SAMPLE), self.SALT
        )

    def test_returns_64_char_hex(self):
        h = hash_phone_e164(self.SAMPLE, self.SALT)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
