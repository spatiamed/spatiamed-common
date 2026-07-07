from sm_common.compliance import (
    ALLOWED_END,
    ALLOWED_START,
    IST,
    MAX_CALLS_PER_DAY,
    MAX_CALLS_PER_WEEK,
)


class TestTCCCPRRules:
    def test_rules_values(self):
        assert MAX_CALLS_PER_DAY == 1
        assert MAX_CALLS_PER_WEEK == 3

    def test_allowed_window(self):
        assert (ALLOWED_START.hour, ALLOWED_START.minute) == (9, 0)
        assert (ALLOWED_END.hour, ALLOWED_END.minute) == (21, 0)

    def test_ist_timezone(self):
        assert str(IST) == "Asia/Kolkata"
