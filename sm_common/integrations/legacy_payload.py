"""The pre-0.12 dict payload, for adapters whose vendors were never measured.

bahmni, mocdoc and generic_rest keep sending exactly what they sent before
AppointmentWrite existed; this refactor must not change their wire behaviour.
"""

from __future__ import annotations

from sm_common.integrations.canonical_types import AppointmentWrite


def legacy_payload(write: AppointmentWrite) -> dict:  # type: ignore[type-arg]
    return {
        "resourceType": "Appointment",
        "status": "booked",
        "appointment_id": str(write.booking_id),
        "slot_start": write.start.isoformat(),
        "slot_end": write.end.isoformat(),
        "doctor_external_id": write.practitioner_ref,
        "patient_external_id": write.patient_ref,
        "reason": write.reason,
    }
