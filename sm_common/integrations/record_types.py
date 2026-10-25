"""Vendor-neutral patient-record types used by record clients (Bahmni first)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from uuid import UUID

Gender = Literal["M", "F", "O"]


@dataclass(frozen=True)
class RecordAddress:
    line1: str = ""
    city_village: str = ""
    district: str = ""
    state: str = ""
    pincode: str = ""


@dataclass(frozen=True)
class RecordPatientInput:
    """A NEW patient to register. ``marker`` is our patients.id (idempotency key)."""

    marker: UUID
    given: str
    family: str
    gender: Gender
    birth_date: date
    birth_date_estimated: bool
    phone: str | None
    address: RecordAddress
    exclude_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class RecordPatient:
    uuid: str
    identifier: str
    given: str
    family: str
    gender: Gender | None
    birth_date: date | None
    # None = unknown (Bahmni search hits do not say); the profile read carries it.
    birth_date_estimated: bool | None
    age_years: int | None
    phone: str | None
    address: RecordAddress
    dead: bool


@dataclass(frozen=True)
class RecordPatientPatch:
    """Changed demographics only; ``None`` means "leave as is"."""

    given: str | None = None
    family: str | None = None
    gender: Gender | None = None
    birth_date: date | None = None
    birth_date_estimated: bool | None = None
    phone: str | None = None
    address: RecordAddress | None = None


@dataclass(frozen=True)
class RecordVisit:
    uuid: str
    visit_type: str
    start: datetime
    stop: datetime | None
    location_uuid: str


@dataclass(frozen=True)
class RecordObs:
    """``key`` is a key of the integration's concept map (e.g. ``chief_complaint``)."""

    key: str
    value: str | float


@dataclass(frozen=True)
class RecordDiagnosis:
    code: str | None
    title: str
    primary: bool
    code_system: str = "ICD-11"


@dataclass(frozen=True)
class RecordDrugOrder:
    name: str
    dose: str
    frequency: str
    duration_days: int | None
    instructions: str


@dataclass(frozen=True)
class RecordEncounterInput:
    """One clinical note as an encounter. ``note_id`` is the idempotency key."""

    note_id: UUID
    patient_uuid: str
    visit_uuid: str
    provider_uuid: str
    encounter_at: datetime
    obs: list[RecordObs]
    diagnoses: list[RecordDiagnosis]
    drug_orders: list[RecordDrugOrder]


@dataclass(frozen=True)
class RecordEncounterResult:
    encounter_uuid: str
    created: bool


@dataclass(frozen=True)
class RecordEncounterSummary:
    uuid: str
    encounter_type: str
    at: datetime
    provider: str
    obs: list[tuple[str, str]]
    # (title, comments)
    diagnoses: list[tuple[str, str | None]]
    drugs: list[str]


@dataclass(frozen=True)
class RecordVisitDetail:
    visit: RecordVisit
    encounters: list[RecordEncounterSummary]


@dataclass(frozen=True)
class RecordService:
    uuid: str
    name: str
    duration_min: int | None


@dataclass(frozen=True)
class RecordDocumentInput:
    patient_uuid: str
    content: bytes
    mime: Literal["application/pdf", "image/jpeg", "image/png"]
    doc_type: str
    provider_uuid: str
    taken_at: datetime


@dataclass(frozen=True)
class RecordDocumentResult:
    visit_uuid: str
    encounter_uuid: str
    url: str
