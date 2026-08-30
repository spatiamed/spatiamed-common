# sm_common/demo_scenario/types.py
"""Value types for the demo-tenant scenario.

The scenario is a *description* of activity, not a writer of it. It holds no DB
handle and makes no HTTP call, so it can be generated in CI and consumed by a
backfill script and a live driver that share no code and no database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

CLINIC_TZ_NAME = "Asia/Kolkata"

DEPARTMENT_CODES = ("GENM", "PEDS", "DERM")
DEPARTMENT_WEIGHTS = (0.50, 0.30, 0.20)

OUTCOMES = ("completed", "no_show", "cancelled", "skipped")
OUTCOME_WEIGHTS = (0.82, 0.09, 0.06, 0.03)


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    tenant_id: uuid.UUID
    start: date
    end: date
    seed: int = 20260829
    roster_size: int = 600
    weekday_tokens: int = 40
    saturday_tokens: int = 15
    returning_share: float = 0.35


@dataclass(frozen=True, slots=True)
class PatientRef:
    index: int
    patient_id: uuid.UUID
    name: str
    phone: str
    age: int
    gender: str  # "M" | "F"
    language: str  # "en" | "hi" | "mr"


@dataclass(frozen=True, slots=True)
class TokenPlan:
    sequence_number: int
    patient_index: int
    department_code: str
    doctor_slot: int  # index into that department's doctor list
    arrival: datetime  # tz-aware, UTC
    called_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    outcome: str
    payment_method: str | None  # "cash" | "upi" | "card"
    amount_inr: int
    feedback_rating: int | None
    note_text: str | None
    diagnosis_term: str | None
    icd_code: str | None


@dataclass(frozen=True, slots=True)
class DayPlan:
    day: date
    tokens: tuple[TokenPlan, ...] = field(default_factory=tuple)
