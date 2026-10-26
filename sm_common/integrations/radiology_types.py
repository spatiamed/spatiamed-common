"""Value types for the Bahmni radiology client (``adapters/bahmni_radiology.py``).

Kept apart from ``record_types`` so the radiology surface ships as a standalone
module: callers import these by module path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class RadiologyProcedure:
    """An orderable imaging procedure (a concept in the configured orderable set).

    ``pacs_mapped`` is False when the concept has no "PACS Procedure Code" mapping:
    Bahmni's imaging bridge then never forwards an order for it to the imaging system.
    """

    concept_uuid: str
    name: str
    pacs_code: str | None
    pacs_mapped: bool


@dataclass(frozen=True)
class RadiologyOrderWrite:
    concept_uuid: str
    urgency: Literal["ROUTINE", "STAT"] = "ROUTINE"
    comment_to_fulfiller: str | None = None  # clinical indication, <= 1024 chars


@dataclass(frozen=True)
class PlacedRadiologyOrder:
    order_uuid: str
    order_number: str  # the imaging accession number
    concept_uuid: str
    encounter_uuid: str


@dataclass(frozen=True)
class RadiologyOrderRow:
    order_uuid: str
    order_number: str
    concept_uuid: str
    action: str
    date_activated: datetime
    date_stopped: datetime | None


@dataclass(frozen=True)
class RadiologyReportOutcome:
    encounter_uuid: str
    obs_uuid: str


@dataclass(frozen=True)
class PacsStudy:
    study_instance_uid: str
    accession: str | None
    modality: str | None
    study_at: datetime | None
    description: str | None


@dataclass(frozen=True)
class RadiologyDocumentRef:
    encounter_uuid: str
    file_url: str
