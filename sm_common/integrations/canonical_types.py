from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from uuid import UUID


# ─── Inbound: HMS → QueueCare ────────────────────────────────────────────────


@dataclass
class AppointmentCreated:
    event_uuid: UUID
    hms_vendor: str
    appointment_id: str
    hms_version: int
    mrn: str
    abha_id: str | None
    phone_hash: str
    patient_name_token: str  # opaque encrypted token (consumer-side field encryption)
    patient_age: int | None
    patient_gender: Literal["M", "F", "O"] | None
    slot_start: datetime
    slot_duration_min: int
    doctor_external_id: str
    department_external_id: str
    payer_type: str
    reason_text: str | None
    received_at: datetime


@dataclass
class AppointmentRescheduled:
    event_uuid: UUID
    hms_vendor: str
    appointment_id: str
    hms_version: int
    new_slot_start: datetime
    new_doctor_external_id: str | None
    received_at: datetime


@dataclass
class AppointmentCancelled:
    event_uuid: UUID
    hms_vendor: str
    appointment_id: str
    hms_version: int
    cancelled_by: Literal["patient", "hospital", "system"]
    reason: str | None
    received_at: datetime


# ─── Outbound: QueueCare → HMS ────────────────────────────────────────────────


@dataclass
class VisitCheckedIn:
    event_uuid: UUID
    appointment_id: str
    queuecare_visit_id: UUID
    arrived_at: datetime
    token_number: str


@dataclass
class VisitConsultationStarted:
    event_uuid: UUID
    appointment_id: str
    started_at: datetime
    actual_doctor_external_id: str


@dataclass
class VisitFinalized:
    event_uuid: UUID
    appointment_id: str
    final_status: Literal["completed", "no_show", "cancelled_after_arrival"]
    finalized_at: datetime


# ─── Supporting types ─────────────────────────────────────────────────────────


@dataclass
class CanonicalPatient:
    mrn: str
    abha_id: str | None
    phone_hash: str
    name_token: str  # Fernet-encrypted
    age: int | None
    gender: Literal["M", "F", "O"] | None
    # The vendor's own resource id (FHIR `Patient.id`). This — not `mrn` — is
    # what external_patient_refs.external_patient_id stores: inbound ingest
    # reads the Patient reference off an Appointment, which is always the
    # resource id. On OpenEMR the first identifier is the internal pid, a
    # different value, so using mrn gave one patient two conflicting refs.
    resource_id: str | None = None
    # Real DOB when the vendor holds one (FHIR birthDate, partial allowed).
    # birth_date is the stored anchor for partials (15th / 1 July, see
    # sm_common.identity.dob); birth_date_precision is exact|month|year.
    # mocdoc/bahmni/generic_rest are age-only and leave both None; QueueCare
    # ingest turns such an age into an `estimated` DOB.
    birth_date: date | None = None
    birth_date_precision: str | None = None


@dataclass
class CanonicalDoctor:
    external_doctor_id: str
    external_speciality_id: str
    external_department_id: str
    external_sub_dept_id: str | None
    speciality_label: str
    department_label: str
    consultation_fee_inr: int | None
    consultation_duration_min: int
    languages: list[str] = field(default_factory=list)
    # Roster identity (v0.13.0) — what an admin maps a local doctor by.
    # Filled by FhirR4Adapter; bahmni/generic_rest/mocdoc leave the defaults.
    # identifiers are (system, value); system is "" when the vendor sent none.
    display_name: str | None = None
    identifiers: list[tuple[str, str]] = field(default_factory=list)
    active: bool | None = None


@dataclass
class CanonicalAppointment:
    appointment_id: str
    hms_version: int
    patient: CanonicalPatient
    doctor_external_id: str
    department_external_id: str
    slot_start: datetime
    slot_duration_min: int
    payer_type: str
    reason_text: str | None
    status: str


@dataclass(frozen=True)
class AppointmentWrite:
    """One booking, in the shape every adapter translates into its vendor's own.

    ``booking_id`` is ours and is every adapter's idempotency marker.
    ``patient_ref`` / ``practitioner_ref`` are the HMS's FHIR resource ids.
    """

    booking_id: UUID
    patient_ref: str
    practitioner_ref: str | None
    start: datetime
    end: datetime
    reason: str | None

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("AppointmentWrite start/end must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("AppointmentWrite end must be after start")


@dataclass
class WriteBackResult:
    status: Literal["SUCCESS", "CONFLICT", "TRANSIENT_ERROR"]
    hms_booking_id: str | None = None
    error_detail: str | None = None
    # The time the HMS actually recorded. QueueCare compares it with its own
    # slot_start before recording success (PR #215 blocker 1).
    hms_start: datetime | None = None
    # False when this call found the appointment we already wrote.
    created: bool = True


@dataclass
class CancelResult:
    status: Literal["SUCCESS", "NOT_FOUND", "FAILED"]
    error_detail: str | None = None


@dataclass
class AdapterHealth:
    healthy: bool
    last_success_at: datetime | None
    latency_ms: int | None
    message: str


@dataclass
class ExternalBooking:
    appointment_id: str
    doctor_external_id: str
    slot_start: datetime
    status: str
    updated_at: datetime
