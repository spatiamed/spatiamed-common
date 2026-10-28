"""Vendor-neutral patient-record types used by record clients (Bahmni first)."""

from __future__ import annotations

from collections.abc import Mapping
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


OrderKind = Literal["drug", "lab", "other"]
CareSetting = Literal["OUTPATIENT", "INPATIENT"]
# "structured": Bahmni's dose/frequency fields; "administration_json": parsed from
# the dosing-instructions JSON (our own pushes); "none": neither was usable.
DosingSource = Literal["structured", "administration_json", "none"]
# OpenMRS order fulfiller statuses, written through the order's ``fulfillerdetails``
# sub-resource (platform 2.2+ for status and comment; ``accessionNumber`` needs 2.3+). A mirror for other
# Bahmni consumers; the caller keeps the authoritative status.
FulfillerStatus = Literal[
    "RECEIVED", "IN_PROGRESS", "EXCEPTION", "ON_HOLD", "DECLINED", "COMPLETED"
]


@dataclass(frozen=True)
class RecordOrder:
    """One OpenMRS order as read back from the record (drug, lab or other)."""

    order_uuid: str
    kind: OrderKind
    # drugNonCoded, else the drug's name, else the concept's display.
    display_name: str
    # None for non-coded drugs: their concept is the install's "other drug" concept.
    concept_uuid: str | None
    drug_uuid: str | None
    encounter_uuid: str
    visit_uuid: str
    care_setting: CareSetting
    dose: float | None
    dose_units: str | None
    frequency: str | None
    duration: int | None
    duration_units: str | None
    # As recorded; a pushed order carries a placeholder the caller must judge.
    quantity: float | None
    quantity_units: str | None
    instructions: str | None
    orderer_provider_uuid: str | None
    orderer_name: str | None
    date_activated: datetime
    date_stopped: datetime | None
    # NEW | REVISE | DISCONTINUE (RENEW on some builds)
    action: str
    previous_order_uuid: str | None
    # Set by the lab feed (= the encounter uuid in Bahmni).
    accession_number: str | None
    fulfiller_status: str | None
    dosing_source: DosingSource
    # The prescribed dose as text when it came from the JSON (e.g. "1 tab"); ``dose``
    # is set only when that text is a plain number.
    dose_text: str | None
    # Bahmni stamps every encounter order with an expiry (the encounter session
    # length). Only the duplicate check honours it; "active" above does not.
    auto_expire_date: datetime | None = None


LabUrgency = Literal["ROUTINE", "STAT"]


@dataclass(frozen=True)
class RecordLabTest:
    """One orderable lab test or panel from the configured orderable concept set."""

    concept_uuid: str
    name: str
    is_panel: bool
    # The name of the "Sample"-class set the test is listed under in the orderable
    # set, when it is listed under one; None otherwise.
    sample_type: str | None


@dataclass(frozen=True)
class RecordLabOrder:
    """A lab test (or panel) to order with the encounter; ``concept_uuid`` is coded."""

    concept_uuid: str
    urgency: LabUrgency = "ROUTINE"
    # Sent as the order's comment to the lab (at most 1024 characters).
    comment: str = ""


@dataclass(frozen=True)
class RecordEncounterInput:
    """One clinical note as an encounter. ``note_id`` is the idempotency key.

    ``encounter_type_uuid`` overrides the configured consultation encounter type
    (e.g. ``discharge_summary_encounter_type_uuid`` for a discharge summary).

    ``encounter_uuid`` is the uuid the encounter is created under; ``None`` uses
    ``note_id``. It is always sent: Bahmni honours it, so each note is its own
    encounter and is never merged into another one.
    """

    note_id: UUID
    patient_uuid: str
    visit_uuid: str
    provider_uuid: str
    encounter_at: datetime
    obs: list[RecordObs]
    diagnoses: list[RecordDiagnosis]
    drug_orders: list[RecordDrugOrder]
    encounter_type_uuid: str | None = None
    encounter_uuid: UUID | None = None
    # A concept with an unexpired OUTPATIENT lab order active in the visit is not
    # re-sent; the result's ``skipped_lab_concepts`` names it.
    lab_orders: list[RecordLabOrder] = field(default_factory=list)


@dataclass(frozen=True)
class RecordEncounterResult:
    encounter_uuid: str
    created: bool
    # Concepts of ``lab_orders`` not sent because an unexpired order for the same
    # concept is already active in the visit (input order, each once).
    skipped_lab_concepts: tuple[str, ...] = ()


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


# Bahmni bed status value as sent, e.g. "AVAILABLE", "OCCUPIED".
BedStatus = str


@dataclass(frozen=True)
class RecordBed:
    # bed_id is the durable key; bed_uuid is as of the read (Bahmni rewrites it on unassign).
    bed_id: int
    bed_uuid: str
    bed_number: str
    bed_type: str
    status: BedStatus
    row: int
    column: int
    # Bahmni does not lock a bed to one patient, so this can hold more than one.
    patient_uuids: tuple[str, ...]


@dataclass(frozen=True)
class RecordWard:
    uuid: str
    name: str
    total_beds: int
    occupied_beds: int
    # Empty on list_wards; get_ward fills it from the bed layout.
    beds: tuple[RecordBed, ...]


@dataclass(frozen=True)
class RecordBedAssignment:
    bed_id: int
    bed_uuid: str
    bed_number: str
    bed_type: str
    # The location the bed is mapped to (may be a child of the admission location).
    ward_uuid: str
    ward_name: str


# Bahmni's three ADT worklists (emrapi.sqlSearch.* saved queries).
AdtList = Literal["to_admit", "admitted", "to_discharge"]


@dataclass(frozen=True)
class RecordAdtRow:
    patient_uuid: str
    identifier: str
    name: str
    visit_uuid: str
    # Only present when the install's worklist query returns those columns.
    ward: str | None
    bed_number: str | None
    advised_at: datetime | None
    # Every other column, as text.
    extra: Mapping[str, str]


@dataclass(frozen=True)
class RecordAdtInput:
    """One ADT event (admit / transfer / discharge). ``event_id`` is the idempotency key.

    ``visit_uuid``: the open visit the caller saw (admit) or the admission's visit
    (transfer, discharge); sent as ``visitUuid``. ``location_uuid`` is the encounter's
    location only: a visit started for an admission uses the configured Visit Location
    (``visit_location_uuid``, else the login location).
    ``at`` is the caller's event time: Bahmni stamps ADT encounters and bed assignments
    with its own time. ``bed_id`` names the bed; ``bed_uuid`` is informational only:
    Bahmni (bedmanagement 6.0.0) rewrites a bed's uuid whenever a patient leaves it.
    """

    event_id: UUID
    patient_uuid: str
    provider_uuid: str
    location_uuid: str
    bed_id: int
    # Optional and informational: not used to find the bed (bed_id is the key).
    bed_uuid: str | None
    at: datetime
    visit_uuid: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class RecordAdmitResult:
    visit_uuid: str
    # May be shared with other writes: Bahmni can merge encounters of one session.
    encounter_uuid: str
    bed: RecordBedAssignment
    # False when a retry found this event's encounter already written.
    created_encounter: bool
    # True only when this call started the visit (start or IPD conversion).
    new_visit: bool
    # True when a retry found the admission in a visit that has since closed.
    visit_stopped: bool
