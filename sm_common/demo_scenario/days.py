# sm_common/demo_scenario/days.py
"""Expand one calendar day into a list of token plans.

Arrival times follow a two-humped OPD curve (mid-morning and late-afternoon)
rather than a flat spread, because flat arrivals make the queue-wait analytics
look synthetic at a glance.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .calendar import day_rng, day_token_count
from .roster import pick_patient_index
from .types import (
    CLINIC_TZ_NAME,
    DEPARTMENT_CODES,
    DEPARTMENT_WEIGHTS,
    OUTCOME_WEIGHTS,
    OUTCOMES,
    DayPlan,
    ScenarioConfig,
    TokenPlan,
)

CLINIC_TZ = ZoneInfo(CLINIC_TZ_NAME)
DAY_START_HOUR = 9
DAY_END_HOUR = 17
DOCTORS_PER_DEPARTMENT = {"GENM": 2, "PEDS": 1, "DERM": 1}
CONSULT_FEE_INR = {"GENM": 400, "PEDS": 500, "DERM": 700}

_NOTES = (
    "Fever for three days, no rash. Advised hydration and paracetamol.",
    "Dry cough, mild throat congestion. Symptomatic management advised.",
    "Routine follow-up. Vitals stable, continuing current medication.",
    "Itchy rash on forearm. Topical steroid prescribed, review in a week.",
    "Child immunisation visit. No adverse reaction observed.",
)
_DIAGNOSES = (
    ("Acute viral fever", "1D44"),
    ("Acute upper respiratory infection", "CA07"),
    ("Contact dermatitis", "EK00"),
    ("Essential hypertension", "BA00"),
    ("Type 2 diabetes mellitus", "5A11"),
)
_PAYMENT_METHODS = ("cash", "upi", "card")


def _arrival_minute(rng: random.Random) -> int:
    """Minutes after 09:00, drawn from a two-humped clinic-day curve."""
    total = (DAY_END_HOUR - DAY_START_HOUR) * 60
    hump = rng.choice((150.0, 420.0))  # ~11:30 and ~16:00
    return int(min(total - 1, max(0, rng.gauss(hump, 70.0))))


def generate_day(cfg: ScenarioConfig, day: date) -> DayPlan:
    count = day_token_count(cfg, day)
    if count == 0:
        return DayPlan(day=day, tokens=())

    rng = day_rng(cfg, day, salt="tokens")
    departments = rng.choices(DEPARTMENT_CODES, weights=DEPARTMENT_WEIGHTS, k=count)
    arrivals = sorted(_arrival_minute(rng) for _ in range(count))

    per_department_seq: dict[str, int] = {code: 0 for code in DEPARTMENT_CODES}
    tokens: list[TokenPlan] = []

    for code, minute in zip(departments, arrivals, strict=True):
        per_department_seq[code] += 1

        local_arrival = datetime.combine(day, time(hour=DAY_START_HOUR), tzinfo=CLINIC_TZ)
        arrival = (local_arrival + timedelta(minutes=minute)).astimezone(UTC)

        outcome = rng.choices(OUTCOMES, weights=OUTCOME_WEIGHTS, k=1)[0]
        called_at = started_at = completed_at = None
        payment_method = None
        note_text = diagnosis_term = icd_code = None
        rating = None

        if outcome in ("completed", "skipped"):
            called_at = arrival + timedelta(minutes=rng.randint(5, 55))
        if outcome == "completed":
            assert called_at is not None  # narrows for mypy: set above for "completed"
            started_at = called_at + timedelta(minutes=rng.randint(0, 4))
            completed_at = started_at + timedelta(minutes=rng.randint(6, 22))
            payment_method = rng.choice(_PAYMENT_METHODS)
            note_text = rng.choice(_NOTES)
            diagnosis_term, icd_code = rng.choice(_DIAGNOSES)
            if rng.random() < 0.30:
                rating = rng.choices((5, 4, 3, 2), weights=(0.55, 0.28, 0.12, 0.05), k=1)[0]

        tokens.append(
            TokenPlan(
                sequence_number=per_department_seq[code],
                patient_index=pick_patient_index(cfg, rng),
                department_code=code,
                doctor_slot=rng.randrange(DOCTORS_PER_DEPARTMENT[code]),
                arrival=arrival,
                called_at=called_at,
                started_at=started_at,
                completed_at=completed_at,
                outcome=outcome,
                payment_method=payment_method,
                amount_inr=CONSULT_FEE_INR[code],
                feedback_rating=rating,
                note_text=note_text,
                diagnosis_term=diagnosis_term,
                icd_code=icd_code,
            )
        )

    return DayPlan(day=day, tokens=tuple(tokens))
