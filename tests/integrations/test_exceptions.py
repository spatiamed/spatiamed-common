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


def test_encounter_terminal_errors_are_exported_with_codes():
    from sm_common.integrations import EncounterMatchAmbiguous, EncounterOutsideVisit
    from sm_common.integrations.exceptions import VendorRejected

    assert EncounterOutsideVisit.code == "encounter_outside_visit"
    assert EncounterMatchAmbiguous.code == "encounter_match_ambiguous"
    assert HmsAdapterError.code is None
    for cls in (EncounterOutsideVisit, EncounterMatchAmbiguous):
        assert issubclass(cls, VendorRejected) and not issubclass(cls, TransientError)


def test_transient_error_carries_status_code():
    assert TransientError("x", status_code=500).status_code == 500
    assert TransientError("x").status_code is None
