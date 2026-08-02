import pytest

from sm_common.identity import assert_distinct_salts


class TestAssertDistinctSalts:
    def test_distinct_salts_ok(self):
        # Returns None, does not raise.
        assert assert_distinct_salts("phone-salt", "email-salt") is None

    def test_three_distinct_ok(self):
        assert assert_distinct_salts("a", "b", "c") is None

    def test_identical_salts_raise(self):
        with pytest.raises(ValueError, match="must be distinct but are"):
            assert_distinct_salts("same", "same")

    def test_identical_among_three_raise(self):
        with pytest.raises(ValueError, match="must be distinct"):
            assert_distinct_salts("a", "b", "a")

    def test_error_uses_names(self):
        with pytest.raises(ValueError, match="'HASH_SALT'.*'PHONE_HASH_SALT'"):
            assert_distinct_salts("dup", "dup", names=["HASH_SALT", "PHONE_HASH_SALT"])

    def test_names_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="labels but"):
            assert_distinct_salts("a", "b", names=["only-one"])

    def test_requires_at_least_two(self):
        with pytest.raises(ValueError, match="at least two"):
            assert_distinct_salts("solo")
