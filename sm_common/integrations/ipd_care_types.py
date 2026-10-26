"""Types for the inpatient care surface: ward board, nursing chart, medication chart.

Beds are identified by ``bed_id`` only. Bahmni's bed management rewrites a bed's
uuid (and clears its bed type) when a patient leaves it, so a ``bed_uuid`` here is
the uuid as of the read that produced it and must never be stored as a key.
All datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
