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

BedStatus = Literal["AVAILABLE", "OCCUPIED"]


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
