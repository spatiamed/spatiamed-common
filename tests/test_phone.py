import pytest

from sm_common.phone import hash_phone, normalize_phone


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
