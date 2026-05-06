import pytest
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4
from sm_common.integrations.canonical_types import (
    AppointmentCreated, AppointmentRescheduled, AppointmentCancelled,
    VisitCheckedIn, VisitConsultationStarted, VisitFinalized,
    CanonicalPatient, CanonicalDoctor, WriteBackResult, CancelResult,
    AdapterHealth, ExternalBooking,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TestCanonicalTypes:
    def test_appointment_created_fields(self):
        evt = AppointmentCreated(
            event_uuid=uuid4(),
            hms_vendor="bahmni",
            appointment_id="APT-001",
            hms_version=1,
            mrn="MRN123",
            abha_id=None,
            phone_hash="abc123",
            patient_name_token="enc:xyz",
            patient_age=35,
            patient_gender="M",
            slot_start=_now(),
            slot_duration_min=15,
            doctor_external_id="DR001",
            department_external_id="DEPT001",
            payer_type="CASH",
            reason_text="Follow-up",
            received_at=_now(),
        )
        assert evt.hms_vendor == "bahmni"
        assert evt.mrn == "MRN123"

    def test_appointment_created_as_dict(self):
        evt = AppointmentCreated(
            event_uuid=uuid4(), hms_vendor="mocdoc", appointment_id="A1",
            hms_version=1, mrn="MRN1", abha_id="12-3456-7890-1234",
            phone_hash="h1", patient_name_token="tok", patient_age=None,
            patient_gender=None, slot_start=_now(), slot_duration_min=15,
            doctor_external_id="D1", department_external_id="DEP1",
            payer_type="CASH", reason_text=None, received_at=_now(),
        )
        d = asdict(evt)
        assert d["appointment_id"] == "A1"

    def test_visit_checked_in(self):
        v = VisitCheckedIn(
            event_uuid=uuid4(), appointment_id="A1",
            queuecare_visit_id=uuid4(), arrived_at=_now(), token_number="T-042",
        )
        assert v.token_number == "T-042"

    def test_write_back_result_success(self):
        r = WriteBackResult(status="SUCCESS", hms_booking_id="HB001")
        assert r.status == "SUCCESS"
        assert r.hms_booking_id == "HB001"
        assert r.error_detail is None

    def test_write_back_result_conflict(self):
        r = WriteBackResult(status="CONFLICT", error_detail="Slot taken")
        assert r.status == "CONFLICT"

    def test_adapter_health_healthy(self):
        h = AdapterHealth(healthy=True, last_success_at=_now(), latency_ms=45, message="OK")
        assert h.healthy is True

    def test_adapter_health_unhealthy(self):
        h = AdapterHealth(healthy=False, last_success_at=None, latency_ms=None, message="timeout")
        assert h.healthy is False
        assert h.last_success_at is None
