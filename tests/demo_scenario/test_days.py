# tests/demo_scenario/test_days.py
from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, date

from sm_common.demo_scenario import OUTCOMES, ScenarioConfig, generate_day

CFG = ScenarioConfig(
    tenant_id=uuid.UUID("6ce47f21-64b6-4298-aebb-b8845648c8c0"),
    start=date(2026, 6, 29),
    end=date(2026, 9, 30),
)
MONDAY = date(2026, 6, 29)


def test_sunday_generates_no_tokens():
    assert generate_day(CFG, date(2026, 7, 5)).tokens == ()


def test_day_is_deterministic():
    assert generate_day(CFG, MONDAY) == generate_day(CFG, MONDAY)


def test_sequence_numbers_are_unique_within_a_department():
    day = generate_day(CFG, MONDAY)
    for code in {t.department_code for t in day.tokens}:
        seqs = [t.sequence_number for t in day.tokens if t.department_code == code]
        assert len(seqs) == len(set(seqs))
        assert seqs == sorted(seqs)


def test_all_timestamps_are_utc_aware():
    for t in generate_day(CFG, MONDAY).tokens:
        assert t.arrival.tzinfo is UTC
        for ts in (t.called_at, t.started_at, t.completed_at):
            assert ts is None or ts.tzinfo is UTC


def test_only_completed_tokens_carry_clinical_and_payment_data():
    for t in generate_day(CFG, MONDAY).tokens:
        if t.outcome == "completed":
            assert t.completed_at is not None
            assert t.payment_method is not None
            assert t.note_text and t.icd_code
        else:
            assert t.completed_at is None
            assert t.payment_method is None
            assert t.note_text is None


def test_outcomes_are_drawn_from_the_declared_set():
    assert set(Counter(t.outcome for t in generate_day(CFG, MONDAY).tokens)) <= set(OUTCOMES)


def test_timestamps_are_monotonic_for_completed_tokens():
    for t in generate_day(CFG, MONDAY).tokens:
        if t.outcome == "completed":
            assert t.arrival <= t.called_at <= t.started_at <= t.completed_at
