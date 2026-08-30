# sm_common/demo_scenario/calendar.py
"""Which days the clinic runs, and how busy each one is.

Every function here is a pure function of (config, day) — never of the days
around it. That independence is what lets the daily live driver compute today's
slice without replaying two months of history first.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta

from .types import ScenarioConfig

SUNDAY = 6
SATURDAY = 5


def day_rng(cfg: ScenarioConfig, day: date, salt: str = "") -> random.Random:
    """A generator seeded only by (scenario seed, day, salt).

    Deriving the seed through sha256 rather than hash() keeps it stable across
    interpreter runs regardless of PYTHONHASHSEED.
    """
    material = f"{cfg.seed}:{cfg.tenant_id}:{day.isoformat()}:{salt}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))


def scenario_days(cfg: ScenarioConfig) -> tuple[date, ...]:
    """Every date in the range, inclusive of both ends — closed days included."""
    span = (cfg.end - cfg.start).days
    return tuple(cfg.start + timedelta(days=i) for i in range(span + 1))


def day_token_count(cfg: ScenarioConfig, day: date) -> int:
    """Token volume for one day: 0 on Sunday, a jittered base otherwise."""
    weekday = day.weekday()
    if weekday == SUNDAY:
        return 0
    base = cfg.saturday_tokens if weekday == SATURDAY else cfg.weekday_tokens
    jitter = day_rng(cfg, day, salt="volume").uniform(-0.2, 0.2)
    return max(1, round(base * (1 + jitter)))
