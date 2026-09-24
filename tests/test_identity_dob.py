"""sm_common.identity.dob — the one implementation of DOB arithmetic.

The round-trip "property" tests are exhaustive loops (hypothesis is not a
dependency): every day of a leap and a non-leap year x every age 0..120.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from sm_common.identity.dob import (
    AGE_TOLERANCE_YEARS,
    Dob,
    DobError,
    age_on,
    coarser,
    corroborates,
    estimate_from_age,
    from_age,
    make_dob,
    parse_dob,
    resolve_two_digit_year,
    validate,
)

REF = date(2026, 9, 24)


def _days(year: int, step: int = 1) -> list[date]:
    d, out = date(year, 1, 1), []
    while d.year == year:
        out.append(d)
        d += timedelta(days=step)
    return out


# ── age_on ────────────────────────────────────────────────────────


def test_age_on_birthday_boundary():
    assert age_on(date(1985, 3, 12), date(2026, 3, 11)) == 40
    assert age_on(date(1985, 3, 12), date(2026, 3, 12)) == 41


def test_feb_29_birthday_is_march_1_in_non_leap_years():
    born = date(2000, 2, 29)
    assert age_on(born, date(2025, 2, 28)) == 24
    assert age_on(born, date(2025, 3, 1)) == 25


def test_feb_29_birthday_is_feb_29_in_leap_years():
    born = date(2000, 2, 29)
    assert age_on(born, date(2024, 2, 28)) == 23
    assert age_on(born, date(2024, 2, 29)) == 24


# ── estimate_from_age: the round-trip property ────────────────────


@pytest.mark.parametrize("year", [2024, 2025])  # leap, non-leap
def test_estimate_round_trips_for_every_day_and_every_age(year):
    for d in _days(year):
        for a in range(0, 121):
            est = estimate_from_age(a, d)
            assert age_on(est, d) == a, (d, a, est)
            assert est <= d


def test_estimate_is_six_months_before_the_age_anniversary():
    assert estimate_from_age(60, date(2026, 3, 1)) == date(1965, 9, 1)
    assert estimate_from_age(60, date(2026, 9, 24)) == date(1966, 3, 24)


def test_estimate_clamps_to_month_end_and_feb_29():
    assert estimate_from_age(30, date(2025, 8, 30)) == date(1995, 2, 28)
    assert estimate_from_age(30, date(2026, 8, 29)) == date(1996, 2, 29)
    assert estimate_from_age(30, date(2026, 8, 31)) == date(1996, 2, 29)


@pytest.mark.parametrize("bad", [-1, 121])
def test_estimate_rejects_out_of_range_age(bad):
    with pytest.raises(DobError):
        estimate_from_age(bad, REF)


def test_from_age_is_estimated_precision():
    assert from_age(41, REF) == Dob(estimate_from_age(41, REF), "estimated")


# ── midpoint anchors: tolerance <= 1 ──────────────────────────────


@pytest.mark.parametrize("year", [2024, 2025])
def test_year_anchor_within_one_year_of_true_age(year):
    for d in _days(year, step=5):
        for a in range(0, 100, 3):
            birth_year = d.year - a - 1
            anchor = make_dob(birth_year, None, None, "year", reference=d).value
            for month in range(1, 13):
                true = date(birth_year, month, 28)
                assert abs(age_on(anchor, d) - age_on(true, d)) <= 1


@pytest.mark.parametrize("year", [2024, 2025])
def test_month_anchor_within_one_year_of_true_age(year):
    for d in _days(year, step=5):
        for a in range(0, 100, 7):
            y = d.year - a - 1
            for month in range(1, 13):
                anchor = make_dob(y, month, None, "month", reference=d).value
                for day in (1, 14, 16, 28):
                    assert abs(age_on(anchor, d) - age_on(date(y, month, day), d)) <= 1


# ── parse_dob ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1985-03-12", Dob(date(1985, 3, 12), "exact")),
        ("1985-03", Dob(date(1985, 3, 15), "month")),
        ("1985", Dob(date(1985, 7, 1), "year")),
        ("12-03-1985", Dob(date(1985, 3, 12), "exact")),
        ("12/03/1985", Dob(date(1985, 3, 12), "exact")),
        ("12.03.1985", Dob(date(1985, 3, 12), "exact")),
        (" 1985-03-12 ", Dob(date(1985, 3, 12), "exact")),
    ],
)
def test_parse_shapes(raw, expected):
    assert parse_dob(raw, reference=REF) == expected


def test_explicit_precision_must_match_the_shape():
    assert parse_dob("1985", reference=REF, precision="year").precision == "year"
    with pytest.raises(DobError):
        parse_dob("1985-03-12", reference=REF, precision="year")
    with pytest.raises(DobError):
        parse_dob("1985", reference=REF, precision="estimated")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12/03/85", date(1985, 3, 12)),
        ("12/03/05", date(2005, 3, 12)),
        ("01/01/26", date(2026, 1, 1)),
        ("30/12/26", date(1926, 12, 30)),  # 2026-12-30 is after REF
    ],
)
def test_two_digit_year_is_the_most_recent_year_not_after_reference(raw, expected):
    assert parse_dob(raw, reference=REF).value == expected


def test_resolve_two_digit_year_direct():
    assert resolve_two_digit_year(26, month=9, day=24, reference=REF) == 2026
    assert resolve_two_digit_year(26, month=9, day=25, reference=REF) == 1926


@pytest.mark.parametrize(
    "raw",
    [
        "1985-02-30",
        "31/04/1990",
        "not a date",
        "",
        "85",
        "2027",
        "2026-10",
        "2026-09-25",
        "1905-09-24",
    ],
)
def test_invalid_dobs_raise(raw):
    with pytest.raises(DobError):
        parse_dob(raw, reference=REF)


def test_120_years_is_the_limit():
    assert age_on(parse_dob("1906-09-24", reference=REF).value, REF) == 120


def test_current_year_only_is_valid_age_zero():
    """R11 / Review Focus 1: a baby born this year, parent knows only the year."""
    early = date(2026, 3, 1)
    dob = parse_dob("2026", reference=early)
    assert dob == Dob(early, "year")  # 1 July anchor clamped to the reference day
    assert age_on(dob.value, early) == 0


def test_current_month_only_is_valid_age_zero():
    d = date(2026, 9, 10)
    dob = parse_dob("2026-09", reference=d)
    assert dob == Dob(d, "month")
    assert age_on(dob.value, d) == 0


def test_validate_rejects_future_and_ancient():
    with pytest.raises(DobError):
        validate(Dob(date(2026, 9, 25), "exact"), reference=REF)
    with pytest.raises(DobError):
        validate(Dob(date(1900, 1, 1), "exact"), reference=REF)
    validate(Dob(date(1985, 3, 12), "exact"), reference=REF)


def test_wire_format():
    assert Dob(date(1985, 7, 1), "year").wire() == "1985"
    assert Dob(date(1985, 3, 15), "month").wire() == "1985-03"
    assert Dob(date(1985, 3, 12), "exact").wire() == "1985-03-12"


# ── corroborates: the coarser side decides ───────────────────────

TRUE = date(1985, 3, 12)
OTHER = date(1991, 11, 2)


def _as(precision: str, born: date) -> Dob:
    if precision == "exact":
        return Dob(born, "exact")
    if precision == "month":
        return make_dob(born.year, born.month, None, "month", reference=REF)
    if precision == "year":
        return make_dob(born.year, None, None, "year", reference=REF)
    return from_age(age_on(born, REF), REF)


PRECISIONS_4 = ["exact", "month", "year", "estimated"]


@pytest.mark.parametrize("pa", PRECISIONS_4)
@pytest.mark.parametrize("pb", PRECISIONS_4)
def test_same_person_corroborates_over_full_matrix(pa, pb):
    assert corroborates(_as(pa, TRUE), _as(pb, TRUE), on_day=REF) is True


@pytest.mark.parametrize("pa", PRECISIONS_4)
@pytest.mark.parametrize("pb", PRECISIONS_4)
def test_different_person_disagrees_over_full_matrix(pa, pb):
    assert corroborates(_as(pa, TRUE), _as(pb, OTHER), on_day=REF) is False


def test_anchors_are_never_compared():
    # year anchor is 1 July, month anchor is the 15th; the components agree.
    assert corroborates(Dob(date(1985, 7, 1), "year"), Dob(date(1985, 12, 15), "month"), on_day=REF)
    assert corroborates(
        Dob(date(1985, 3, 15), "month"), Dob(date(1985, 3, 30), "exact"), on_day=REF
    )


def test_exact_vs_exact_any_difference_vetoes():
    assert (
        corroborates(Dob(date(1985, 3, 12), "exact"), Dob(date(1985, 3, 13), "exact"), on_day=REF)
        is False
    )


def test_estimated_tolerance_is_two_years():
    stored = from_age(41, REF)
    assert corroborates(
        stored, Dob(date(REF.year - 41 - AGE_TOLERANCE_YEARS, 1, 1), "exact"), on_day=REF
    )
    far = Dob(date(REF.year - 41 - AGE_TOLERANCE_YEARS - 2, 1, 1), "exact")
    assert corroborates(stored, far, on_day=REF) is False


def test_unknown_side_is_none_not_false():
    assert corroborates(None, Dob(TRUE, "exact"), on_day=REF) is None
    assert corroborates(Dob(TRUE, "exact"), None, on_day=REF) is None


def test_coarser():
    assert coarser("exact", "estimated") == "estimated"
    assert coarser("month", "year") == "year"
    assert coarser("exact", "month") == "month"


# ── package compatibility ─────────────────────────────────────────


def test_identity_package_still_exports_assert_distinct_salts():
    from sm_common import assert_distinct_salts as top
    from sm_common.identity import assert_distinct_salts as pkg

    assert top is pkg
