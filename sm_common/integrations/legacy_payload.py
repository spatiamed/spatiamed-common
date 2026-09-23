"""The dict payload for bahmni, mocdoc and generic_rest, built from AppointmentWrite.

None of these vendors was ever measured, and this is NOT wire-identical to what
QueueCare sent before 0.12. Against that pre-0.12 dict:

- ``hms_vendor`` is dropped;
- ``slot_end`` and ``reason`` are added;
- ``doctor_external_id`` changed meaning: it was QueueCare's local staff UUID
  and is now the HMS practitioner reference (``AppointmentWrite.practitioner_ref``,
  None until the doctor is mapped).

``resourceType``, ``status``, ``appointment_id``, ``slot_start`` and
``patient_external_id`` are unchanged. Measure a vendor before relying on it.
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
