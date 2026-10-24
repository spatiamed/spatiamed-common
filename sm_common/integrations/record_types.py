"""Vendor-neutral patient-record types used by record clients (Bahmni first)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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
    birth_date_estimated: bool
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
