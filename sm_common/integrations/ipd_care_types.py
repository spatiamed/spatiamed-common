"""Types for the inpatient care surface: ward board, nursing chart, medication chart.

Beds are identified by ``bed_id`` only. Bahmni's bed management rewrites a bed's
uuid (and clears its bed type) when a patient leaves it, so a ``bed_uuid`` here is
the uuid as of the read that produced it and must never be stored as a key.
All datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

BedStatus = Literal["AVAILABLE", "OCCUPIED"]
SlotStatus = Literal["SCHEDULED", "NOT_DONE", "COMPLETED", "STOPPED", "MISSED"]
SlotServiceType = Literal[
    "MedicationRequest",
    "EmergencyMedicationRequest",
    "AsNeededMedicationRequest",
    "AsNeededPlaceholder",
]
AdministrationStatus = Literal["completed", "not-done", "stopped"]


@dataclass(frozen=True)
class WardBed:
    bed_id: int
    # Valid only as of this read; never a key.
    bed_uuid: str | None
    bed_number: str
    # Bed type name (what bed creation takes); None when Bahmni has cleared it.
    bed_type: str | None
    row: int
    column: int
    status: BedStatus
    patient_uuids: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True)
class WardLayout:
    ward_uuid: str
    ward_name: str
    # Grid size over every layout cell, including cells with no bed.
    rows: int
    columns: int
    beds: tuple[WardBed, ...]


@dataclass(frozen=True)
class WardPatient:
    patient_uuid: str
    visit_uuid: str
    bed_id: int | None
    bed_number: str | None
    admitted_at: datetime | None
    display_name: str


@dataclass(frozen=True)
class VitalsWrite:
    """One set of vitals; unset fields are not written. At least one must be set."""

    pulse: int | None = None
    bp_systolic: int | None = None
    bp_diastolic: int | None = None
    temp_c: Decimal | None = None
    spo2: int | None = None
    resp_rate: int | None = None
    weight_kg: Decimal | None = None
    height_cm: Decimal | None = None


@dataclass(frozen=True)
class IntakeOutputWrite:
    kind: Literal["intake", "output"]
    # Free text, e.g. "Oral", "IV", "Urine", "Drain".
    route: str
    quantity_ml: int
    # When the intake was taken or the output measured (aware UTC).
    recorded_at: datetime


@dataclass(frozen=True)
class NursingWriteOutcome:
    encounter_uuid: str
    # Top-level obs of the write (an intake/output entry is one group obs).
    obs_uuids: tuple[str, ...]
    # False when an earlier write with the same client_ref was found instead.
    created: bool


@dataclass(frozen=True)
class MarSlot:
    """One medication-chart slot: a due dose of one drug order."""

    slot_uuid: str
    order_uuid: str
    drug_name: str | None
    service_type: SlotServiceType
    start_at: datetime
    status: SlotStatus
    # The administration recorded on the slot, if any.
    administration_uuid: str | None
    administered_at: datetime | None
    notes: str | None


@dataclass(frozen=True)
class MarScheduleWrite:
    """Put one active drug order on the medication chart.

    Every instant is timezone-aware. ``first_day_slots`` are the doses left on the
    first day, ``day_wise_slots`` the dose instants of the first full day (the record
    repeats them every 24 hours) and ``remaining_day_slots`` the doses of the last day.
    The record creates ``ceil(quantity / dose)`` slots in total and drops any time past
    that count. An as-needed order is charted with no slots. The record charts against
    the patient's active visit; no visit is named here.
    """

    patient_uuid: str
    order_uuid: str
    provider_uuid: str
    first_day_slots: tuple[datetime, ...]
    day_wise_slots: tuple[datetime, ...]
    remaining_day_slots: tuple[datetime, ...] = ()
    service_type: Literal["MedicationRequest", "AsNeededMedicationRequest"] = "MedicationRequest"
    comments: str | None = None


@dataclass(frozen=True)
class MarScheduleOutcome:
    order_uuid: str
    # False when the order was already on the chart (nothing sent).
    created: bool
    slot_uuids: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class MarAdministrationWrite:
    """One dose (or a dose not given) recorded against an active drug order.

    ``slot_uuid`` names the chart slot for a scheduled dose and is ``None`` for an
    as-needed dose. ``status_reason`` is required for ``not-done``; the record keeps it
    only as a note. ``dose_units`` and ``route`` are concept NAMES (the record resolves
    them by name), not uuids. ``administered_at`` is timezone-aware. The record files an
    as-needed dose under the patient's active visit; ``visit_uuid`` is the caller's
    reference and is not sent.
    """

    patient_uuid: str
    visit_uuid: str
    encounter_uuid: str | None
    order_uuid: str
    slot_uuid: str | None
    provider_uuids: tuple[str, ...]
    status: AdministrationStatus
    status_reason: str | None
    administered_at: datetime
    dose: Decimal | None = None
    dose_units: str | None = None
    route: str | None = None
    notes: str | None = None
    client_ref: UUID


@dataclass(frozen=True)
class MarAdministrationOutcome:
    administration_uuid: str
    # The chart slot holding the administration (an as-needed dose gets a new one).
    slot_uuid: str | None
    # False when the slot already held an administration (nothing new recorded), or when
    # an earlier write with the same client_ref was found.
    created: bool
    status: AdministrationStatus
    # True when the administration carries this write's client_ref marker: our own
    # earlier write rather than one recorded by someone else.
    ref_matched: bool = False
