# tests/demo_scenario/test_calendar.py
from __future__ import annotations

import uuid
from datetime import date

from sm_common.demo_scenario import ScenarioConfig, day_token_count, scenario_days

CFG = ScenarioConfig(
    tenant_id=uuid.UUID("6ce47f21-64b6-4298-aebb-b8845648c8c0"),
    start=date(2026, 6, 29),
    end=date(2026, 9, 30),
)


def test_sunday_is_closed():
    assert day_token_count(CFG, date(2026, 7, 5)) == 0  # a Sunday


def test_weekday_volume_is_about_forty():
    assert 30 <= day_token_count(CFG, date(2026, 6, 29)) <= 50  # a Monday


def test_saturday_volume_is_about_fifteen():
    assert 8 <= day_token_count(CFG, date(2026, 7, 4)) <= 22  # a Saturday


def test_day_count_is_deterministic():
    day = date(2026, 7, 15)
    assert day_token_count(CFG, day) == day_token_count(CFG, day)


def test_scenario_days_spans_the_range_inclusive():
    days = scenario_days(CFG)
    assert days[0] == date(2026, 6, 29)
    assert days[-1] == date(2026, 9, 30)
    assert len(days) == 94
