from datetime import datetime

import pytz
import pytest

from sm_common.compliance import is_within_allowed_hours, IST, TCCCPR_RULES


class TestIsWithinAllowedHours:
    def test_9am_ist_allowed(self):
        dt = IST.localize(datetime(2026, 4, 8, 9, 0))
        assert is_within_allowed_hours(dt) is True

    def test_9pm_ist_allowed(self):
        dt = IST.localize(datetime(2026, 4, 8, 21, 0))
        assert is_within_allowed_hours(dt) is True

    def test_noon_allowed(self):
        dt = IST.localize(datetime(2026, 4, 8, 12, 0))
        assert is_within_allowed_hours(dt) is True

    def test_830am_rejected(self):
        dt = IST.localize(datetime(2026, 4, 8, 8, 30))
        assert is_within_allowed_hours(dt) is False

    def test_930pm_rejected(self):
        """Critical contract: 9:30 PM IST must be rejected."""
        dt = IST.localize(datetime(2026, 4, 8, 21, 30))
        assert is_within_allowed_hours(dt) is False

    def test_midnight_rejected(self):
        dt = IST.localize(datetime(2026, 4, 8, 0, 0))
        assert is_within_allowed_hours(dt) is False

    def test_naive_datetime_localized_to_ist(self):
        naive = datetime(2026, 4, 8, 12, 0)
        assert is_within_allowed_hours(naive) is True

    def test_utc_converted_to_ist(self):
        # 3:30 AM UTC = 9:00 AM IST
        utc = pytz.utc.localize(datetime(2026, 4, 8, 3, 30))
        assert is_within_allowed_hours(utc) is True

    def test_utc_early_morning_rejected(self):
        # 2:00 AM UTC = 7:30 AM IST (before 9 AM)
        utc = pytz.utc.localize(datetime(2026, 4, 8, 2, 0))
        assert is_within_allowed_hours(utc) is False

    def test_none_uses_current_time(self):
        # Just verify it doesn't raise
        result = is_within_allowed_hours(None)
        assert isinstance(result, bool)


class TestTCCCPRRules:
    def test_rules_values(self):
        assert TCCCPR_RULES["max_calls_per_day"] == 1
        assert TCCCPR_RULES["max_calls_per_week"] == 3
