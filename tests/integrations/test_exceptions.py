from sm_common.integrations.exceptions import (
    AuthError,
    ConflictError,
    HmsAdapterError,
    TransientError,
)


class TestExceptionHierarchy:
    def test_conflict_is_hms_adapter_error(self):
        err = ConflictError("slot taken")
        assert isinstance(err, HmsAdapterError)
        assert isinstance(err, Exception)

    def test_transient_is_hms_adapter_error(self):
        err = TransientError("503")
        assert isinstance(err, HmsAdapterError)

    def test_auth_is_hms_adapter_error(self):
        err = AuthError("401")
        assert isinstance(err, HmsAdapterError)

    def test_conflict_not_transient(self):
        err = ConflictError("slot taken")
        assert not isinstance(err, TransientError)

    def test_message_preserved(self):
        err = ConflictError("slot at 10:30 is booked")
        assert "10:30" in str(err)
