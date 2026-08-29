# sm_common/demo_scenario/roster.py
"""The synthetic patient roster.

Two things here are load-bearing beyond "make up some names":

* `patients.phone_hash` is globally unique with no `hospital_id` column, so the
  roster is deduplicated across the whole platform, not per tenant. A demo number
  that collides with a real patient's would merge the two records. The reserved
  prefix below exists to make that collision impossible by construction.
* Patient ids are `uuid5` of (tenant, index), so re-running the backfill reuses
  rows instead of creating a second roster.
"""

from __future__ import annotations

import random
import uuid

from .types import PatientRef, ScenarioConfig

# Namespace for every deterministic id the scenario mints.
DEMO_NAMESPACE = uuid.UUID("b6f4a1d2-9c3e-4a58-8f21-0d7e5c9a4b13")

# Demo numbers are +91 99999 6xxxx. This is NOT a carrier-reserved range — no such
# range exists in India — so it is a collision guard, not a delivery guard. Nothing
# in phase 1 sends. Before any phase that sends, CareLoop's outbound allowlist fence
# must be on main; see the spec's "Synthetic-data safety" section.
DEMO_PHONE_PREFIX = "+91999996"

_GIVEN_M = ("Arjun", "Rohit", "Imran", "Vikram", "Sanjay", "Karthik", "Aditya", "Farhan")
_GIVEN_F = ("Ananya", "Priya", "Meera", "Fatima", "Divya", "Sneha", "Kavya", "Ritu")
_FAMILY = ("Sharma", "Patil", "Reddy", "Nair", "Kulkarni", "Sheikh", "Iyer", "Bose")
_LANGUAGES = ("en", "hi", "mr")


def patient_uuid(cfg: ScenarioConfig, index: int) -> uuid.UUID:
    return uuid.uuid5(DEMO_NAMESPACE, f"{cfg.tenant_id}:patient:{index}")


def generate_roster(cfg: ScenarioConfig) -> tuple[PatientRef, ...]:
    """Build the full roster. Depends only on tenant_id, seed and roster_size."""
    rng = random.Random(f"{cfg.seed}:{cfg.tenant_id}:roster")
    people: list[PatientRef] = []
    for index in range(cfg.roster_size):
        gender = rng.choice(("M", "F"))
        given = rng.choice(_GIVEN_M if gender == "M" else _GIVEN_F)
        people.append(
            PatientRef(
                index=index,
                patient_id=patient_uuid(cfg, index),
                name=f"{given} {rng.choice(_FAMILY)}",
                # Zero-padded so every number is the same length and unique per index.
                phone=f"{DEMO_PHONE_PREFIX}{index:04d}",
                age=rng.randint(2, 82),
                gender=gender,
                language=rng.choice(_LANGUAGES),
            )
        )
    return tuple(people)


def pick_patient_index(cfg: ScenarioConfig, rng: random.Random) -> int:
    """Choose a patient for one token, biased so some patients return.

    `returning_share` of visits are drawn from the first 15% of the roster — the
    clinic's regulars. The rest are drawn uniformly. Doing it by roster position
    rather than by remembering who came yesterday keeps day generation
    independent, which the live driver depends on.
    """
    regulars = max(1, int(cfg.roster_size * 0.15))
    if rng.random() < cfg.returning_share:
        return rng.randrange(regulars)
    return rng.randrange(cfg.roster_size)
