from datetime import UTC, datetime, timedelta
from uuid import UUID

from sm_common.integrations.canonical_types import AppointmentWrite
from sm_common.integrations.legacy_payload import legacy_payload


def test_legacy_payload_keeps_the_pre_0_12_shape():
    bid = UUID("00000000-0000-0000-0000-0000000000aa")
    start = datetime(2026, 9, 24, 4, 30, tzinfo=UTC)
    w = AppointmentWrite(bid, "EXT-1", "DOC-1", start, start + timedelta(minutes=15), "Fever")
    assert legacy_payload(w) == {
        "resourceType": "Appointment",
        "status": "booked",
        "appointment_id": str(bid),
        "slot_start": start.isoformat(),
        "slot_end": (start + timedelta(minutes=15)).isoformat(),
        "doctor_external_id": "DOC-1",
        "patient_external_id": "EXT-1",
        "reason": "Fever",
    }
