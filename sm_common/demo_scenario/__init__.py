# sm_common/demo_scenario/__init__.py
"""Deterministic activity scenario for the staging demo tenant.

Same config in, byte-identical scenario out. That is the whole contract: it lets
a historical backfill and a live daily driver — different repos, different
databases, no shared state — agree on what happened without talking to each other.
"""

from .calendar import day_rng, day_token_count, scenario_days
from .types import (
    CLINIC_TZ_NAME,
    DEPARTMENT_CODES,
    DEPARTMENT_WEIGHTS,
    OUTCOME_WEIGHTS,
    OUTCOMES,
    DayPlan,
    PatientRef,
    ScenarioConfig,
    TokenPlan,
)

__all__ = [
    "CLINIC_TZ_NAME",
    "DEPARTMENT_CODES",
    "DEPARTMENT_WEIGHTS",
    "OUTCOMES",
    "OUTCOME_WEIGHTS",
    "DayPlan",
    "PatientRef",
    "ScenarioConfig",
    "TokenPlan",
    "day_rng",
    "day_token_count",
    "scenario_days",
]
