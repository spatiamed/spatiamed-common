"""Date-of-birth arithmetic: the one implementation every service shares.

A stored DOB is a (date, precision) pair. Precision says how much of the date
the patient actually knew. For a partial precision the date is a fixed anchor
inside the valid interval: the 15th of the month, or 1 July of the year. For an
age-derived estimate it is the midpoint of the interval the age implies.

Rules (spec 2026-09-24-dob-first-identity-design.md):

* A 29 February birthday falls on 1 March in non-leap years (common Indian practice).
* A two-digit year resolves to the most recent year not after the reference day.
* Valid means a real calendar date, not after the reference day, and at most
  120 years before it. For ``month``/``year`` the check runs on the components;
  an anchor that would land after the reference day (a birth this month or this
  year) is clamped to the reference day.
* Corroboration is decided by the COARSER side's precision. It compares the
  real components, or ``age_on`` values for ``estimated``, and never the anchors.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, get_args

Precision = Literal["exact", "month", "year", "estimated"]
PRECISIONS: Final[tuple[str, ...]] = get_args(Precision)
MAX_AGE_YEARS: Final = 120
# An HMS computes age on a different day than we do, and an estimate is +-6 months
# by construction; +-2 years is today's AGE_TOLERANCE_YEARS in the QueueCare matcher.
AGE_TOLERANCE_YEARS: Final = 2
_COARSENESS: Final[dict[str, int]] = {"exact": 0, "month": 1, "year": 2, "estimated": 3}

_ISO_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_ISO_YEAR = re.compile(r"^(\d{4})$")
_DMY = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})$")


class DobError(ValueError):
    """A DOB that cannot be parsed, or is not a plausible birth date."""


@dataclass(frozen=True, slots=True)
class Dob:
    value: date
    precision: Precision

    def iso(self) -> str:
        """The stored plaintext: always a full YYYY-MM-DD (the anchor for partials)."""
        return self.value.isoformat()

    def wire(self) -> str:
        """What a client sends: YYYY, YYYY-MM or YYYY-MM-DD."""
        if self.precision == "year":
            return f"{self.value.year:04d}"
        if self.precision == "month":
            return f"{self.value.year:04d}-{self.value.month:02d}"
        return self.value.isoformat()


def coarser(a: Precision, b: Precision) -> Precision:
    return a if _COARSENESS[a] >= _COARSENESS[b] else b


def _birthday_in(dob: date, year: int) -> date:
    if dob.month == 2 and dob.day == 29 and not calendar.isleap(year):
        return date(year, 3, 1)
    return date(year, dob.month, dob.day)


def age_on(dob: date, on_day: date) -> int:
    """Completed years on ``on_day``. Precision does not change the arithmetic."""
    years = on_day.year - dob.year
    if on_day < _birthday_in(dob, on_day.year):
        years -= 1
    return years


def estimate_from_age(age: int, on_day: date) -> date:
    """``on_day - age years - 6 months``: the midpoint of (d-(a+1)y, d-a*y].

    The day is clamped to the target month's last day, so 30 August minus
    30y6m is 28 February in a non-leap year and 29 February in a leap year.
    """
    if not 0 <= age <= MAX_AGE_YEARS:
        raise DobError(f"age must be 0..{MAX_AGE_YEARS}")
    year = on_day.year - age
    month = on_day.month - 6
    if month <= 0:
        month += 12
        year -= 1
    day = min(on_day.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def from_age(age: int, on_day: date) -> Dob:
    return Dob(estimate_from_age(age, on_day), "estimated")


def resolve_two_digit_year(yy: int, *, month: int, day: int, reference: date) -> int:
    """The most recent year ending in ``yy`` whose (month, day) is not after ``reference``."""
    if not 0 <= yy <= 99:
        raise DobError("two-digit year out of range")
    year = (reference.year // 100) * 100 + yy
    try:
        candidate = date(year, month, min(day, calendar.monthrange(year, month)[1]))
    except ValueError as exc:
        raise DobError("not a real calendar date") from exc
    return year - 100 if candidate > reference else year


def validate(dob: Dob, *, reference: date) -> None:
    v = dob.value
    if dob.precision == "year":
        future = v.year > reference.year
    elif dob.precision == "month":
        future = (v.year, v.month) > (reference.year, reference.month)
    else:
        future = v > reference
    if future or v > reference:
        raise DobError("date of birth is after the reference day")
    if age_on(v, reference) > MAX_AGE_YEARS:
        raise DobError(f"date of birth is more than {MAX_AGE_YEARS} years ago")


def make_dob(
    year: int, month: int | None, day: int | None, precision: Precision, *, reference: date
) -> Dob:
    """Build and validate a Dob from components. Partial anchors clamp to ``reference`` (R11)."""
    try:
        if precision in ("exact", "estimated"):
            if month is None or day is None:
                raise DobError(f"{precision} needs a full date")
            value = date(year, month, day)
        elif precision == "month":
            if month is None:
                raise DobError("month precision needs a month")
            value = date(year, month, 15)
        else:
            value = date(year, 7, 1)
    except ValueError as exc:
        if isinstance(exc, DobError):
            raise
        raise DobError("not a real calendar date") from exc
    # Components decide "future"; only then may the anchor be clamped.
    if precision == "year" and year > reference.year:
        raise DobError("date of birth is after the reference day")
    if precision == "month" and (year, month or 0) > (reference.year, reference.month):
        raise DobError("date of birth is after the reference day")
    if precision in ("month", "year") and value > reference:
        value = reference
    dob = Dob(value, precision)
    validate(dob, reference=reference)
    return dob


def parse_dob(raw: str, *, reference: date, precision: str | None = None) -> Dob:
    """Parse a client-supplied DOB and validate it against ``reference``.

    Accepts ISO ``YYYY-MM-DD`` / ``YYYY-MM`` / ``YYYY`` and ``DD-MM-YYYY``,
    ``DD/MM/YYYY``, ``DD.MM.YYYY`` (two-digit years allowed there). The shape
    decides the precision; an explicit ``precision`` must agree with it.
    ``estimated`` is never accepted from a client; send an age instead.
    """
    text = (raw or "").strip()
    dob: Dob
    if m := _ISO_FULL.match(text):
        dob = make_dob(int(m[1]), int(m[2]), int(m[3]), "exact", reference=reference)
    elif m := _ISO_MONTH.match(text):
        dob = make_dob(int(m[1]), int(m[2]), None, "month", reference=reference)
    elif m := _ISO_YEAR.match(text):
        dob = make_dob(int(m[1]), None, None, "year", reference=reference)
    elif m := _DMY.match(text):
        day, month, yy = int(m[1]), int(m[2]), m[3]
        if not 1 <= month <= 12:
            raise DobError("not a real calendar date")
        year = (
            int(yy)
            if len(yy) == 4
            else resolve_two_digit_year(int(yy), month=month, day=day, reference=reference)
        )
        dob = make_dob(year, month, day, "exact", reference=reference)
    else:
        raise DobError("unrecognised date of birth")
    if precision is not None and precision != dob.precision:
        raise DobError(f"precision {precision!r} does not match a {dob.precision} date")
    return dob


def corroborates(a: Dob | None, b: Dob | None, *, on_day: date) -> bool | None:
    """Do two independently-reported DOBs describe the same person?

    ``None`` means "unknown" (one side is missing), never a mismatch. Otherwise
    the COARSER side's precision decides what gets compared: the real year,
    the real (year, month), the real date, or — for ``estimated`` — the
    ``age_on`` values within ``AGE_TOLERANCE_YEARS``. Anchors are never
    compared directly, so a year-precision 1 July and a month-precision 15th
    of the same year/month agree.
    """
    if a is None or b is None:
        return None
    precision = coarser(a.precision, b.precision)
    if precision == "estimated":
        return abs(age_on(a.value, on_day) - age_on(b.value, on_day)) <= AGE_TOLERANCE_YEARS
    if precision == "year":
        return a.value.year == b.value.year
    if precision == "month":
        return (a.value.year, a.value.month) == (b.value.year, b.value.month)
    return a.value == b.value
