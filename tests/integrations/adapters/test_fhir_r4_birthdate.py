"""FHIR birthDate is carried through, partial dates included.

`date.fromisoformat` rejects "1985-03" and "1985", so today a partial
birthDate yields age=None and the date is lost. It must arrive as
birth_date + birth_date_precision.
"""

from __future__ import annotations

from datetime import date

import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.canonical_types import CanonicalPatient


def _adapter() -> FhirR4Adapter:
    return FhirR4Adapter(base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"})


def _resource(birth_date: str | None) -> dict:
    r: dict = {"resourceType": "Patient", "id": "p1", "name": [{"text": "Asha Rao"}], "gender": "female"}
    if birth_date is not None:
        r["birthDate"] = birth_date
    return r


@pytest.mark.parametrize(
    "raw, value, precision",
    [
        ("1985-03-12", date(1985, 3, 12), "exact"),
        ("1985-03", date(1985, 3, 15), "month"),
        ("1985", date(1985, 7, 1), "year"),
    ],
)
def test_birth_date_and_precision_carried(raw, value, precision):
    p = _adapter()._patient_to_canonical(_resource(raw))
    assert (p.birth_date, p.birth_date_precision) == (value, precision)
    assert p.age is not None  # still derived (UTC day) for age-only consumers


@pytest.mark.parametrize("raw", [None, "", "garbage", "1985-02-30"])
def test_missing_or_bad_birth_date_is_none(raw):
    p = _adapter()._patient_to_canonical(_resource(raw))
    assert (p.birth_date, p.birth_date_precision, p.age) == (None, None, None)


def test_positional_constructor_sites_still_work():
    p = CanonicalPatient("mrn", None, "hash", "tok", 40, "F", "rid")
    assert (p.resource_id, p.birth_date, p.birth_date_precision) == ("rid", None, None)
