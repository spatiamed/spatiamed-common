# sm_common/demo_scenario/serialise.py
"""Render a scenario to the JSON both drivers read.

The drivers deliberately do NOT import this package — QueueCare and CareLoop pin
different, older spatiamed-common versions, and making the backfill import the
generator would put a cross-repo pin bump on the critical path. JSON is the
interop boundary instead, so `SCHEMA_VERSION` is a real contract: bump it on any
breaking field change and teach the consumers to reject what they cannot read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .calendar import scenario_days
from .days import generate_day
from .roster import generate_roster
from .types import ScenarioConfig

SCHEMA_VERSION = 1


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def scenario_to_dict(cfg: ScenarioConfig) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tenant_id": str(cfg.tenant_id),
        "seed": cfg.seed,
        "start": cfg.start.isoformat(),
        "end": cfg.end.isoformat(),
        "patients": [
            {
                "index": p.index,
                "patient_id": str(p.patient_id),
                "name": p.name,
                "phone": p.phone,
                "age": p.age,
                "gender": p.gender,
                "language": p.language,
            }
            for p in generate_roster(cfg)
        ],
        "days": [
            {
                "day": day.isoformat(),
                "tokens": [
                    {
                        "sequence_number": t.sequence_number,
                        "patient_index": t.patient_index,
                        "department_code": t.department_code,
                        "doctor_slot": t.doctor_slot,
                        "arrival": _ts(t.arrival),
                        "called_at": _ts(t.called_at),
                        "started_at": _ts(t.started_at),
                        "completed_at": _ts(t.completed_at),
                        "outcome": t.outcome,
                        "payment_method": t.payment_method,
                        "amount_inr": t.amount_inr,
                        "feedback_rating": t.feedback_rating,
                        "note_text": t.note_text,
                        "diagnosis_term": t.diagnosis_term,
                        "icd_code": t.icd_code,
                    }
                    for t in generate_day(cfg, day).tokens
                ],
            }
            for day in scenario_days(cfg)
        ],
    }
