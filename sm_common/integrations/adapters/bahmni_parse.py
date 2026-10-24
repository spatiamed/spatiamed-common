"""Parsing helpers shared by the Bahmni adapter and record client."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from sm_common.integrations.adapters.bahmni_http import from_epoch_ms


def gender(raw: Any) -> Literal["M", "F", "O"] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    first = raw.strip()[0].upper()
    return "M" if first == "M" else "F" if first == "F" else "O"


def birth_date(raw: Any) -> date | None:
    """Epoch ms or ``YYYY-MM-DD[...]`` -> the calendar date Bahmni holds.

    Bahmni encodes a birth date as the server-local midnight, so the UTC date of
    the epoch value can be the day before. Rounding to the nearest UTC midnight
    (add 12h, take the date) recovers the stored day for any server offset
    within +-12h. Strings are already local, so their first 10 chars are the date.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        if isinstance(raw, str) and not raw.strip().lstrip("-").isdigit():
            return date.fromisoformat(raw.strip()[:10])
        return (from_epoch_ms(raw) + timedelta(hours=12)).date()
    except (ValueError, TypeError):
        return None


def flag(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.lower() == "true")
