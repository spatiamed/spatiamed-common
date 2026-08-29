# tests/demo_scenario/test_roster.py
from __future__ import annotations

import uuid
from datetime import date

from sm_common.demo_scenario import ScenarioConfig, day_rng
from sm_common.demo_scenario.roster import DEMO_PHONE_PREFIX, generate_roster, pick_patient_index

CFG = ScenarioConfig(
    tenant_id=uuid.UUID("6ce47f21-64b6-4298-aebb-b8845648c8c0"),
    start=date(2026, 6, 29),
    end=date(2026, 9, 30),
)


def test_roster_has_the_configured_size():
    assert len(generate_roster(CFG)) == 600


def test_roster_is_deterministic():
    assert generate_roster(CFG) == generate_roster(CFG)


def test_every_phone_uses_the_demo_prefix():
    assert all(p.phone.startswith(DEMO_PHONE_PREFIX) for p in generate_roster(CFG))


def test_phones_and_ids_are_unique():
    roster = generate_roster(CFG)
    assert len({p.phone for p in roster}) == len(roster)
    assert len({p.patient_id for p in roster}) == len(roster)


def test_patient_ids_are_stable_across_configs_with_the_same_tenant_and_seed():
    other = ScenarioConfig(tenant_id=CFG.tenant_id, start=date(2026, 7, 1), end=date(2026, 7, 2))
    assert generate_roster(other)[0].patient_id == generate_roster(CFG)[0].patient_id


def test_pick_patient_index_stays_in_range():
    rng = day_rng(CFG, date(2026, 7, 1))
    assert all(0 <= pick_patient_index(CFG, rng) < CFG.roster_size for _ in range(200))
