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
    patient_name_token: str   # opaque encrypted token (consumer-side field encryption)
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
    name_token: str          # Fernet-encrypted
    age: int | None
    gender: Literal["M", "F", "O"] | None


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


@dataclass
class WriteBackResult:
    status: Literal["SUCCESS", "CONFLICT", "TRANSIENT_ERROR"]
    hms_booking_id: str | None = None
    error_detail: str | None = None


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
