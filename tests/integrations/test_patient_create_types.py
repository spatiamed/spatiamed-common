from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pytest

from sm_common.integrations import PatientCreate, PatientCreateResult
from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.exceptions import WriteNotSupported


def _pc(**over):
    kw = dict(patient_marker=uuid4(), family="Rao", given=["Asha"], birth_date=date(1985, 3, 12),
              gender="F", phone="9876543210")
    kw.update(over)
    return PatientCreate(**kw)


def test_valid_create_is_normalised():
    p = _pc(family="  Rao ", given=[" Asha ", "", "Devi"])
    assert p.family == "Rao" and p.given == ["Asha", "Devi"] and p.exclude_ids == frozenset()


@pytest.mark.parametrize("family", ["R", " K ", "", "  "])
def test_patient_create_rejects_a_one_letter_surname_after_trim(family):
    with pytest.raises(ValueError):
        _pc(family=family)


def test_birth_date_must_be_a_date_not_a_datetime():
    with pytest.raises(ValueError):
        _pc(birth_date=datetime(1985, 3, 12))


def test_birth_date_rejects_a_string():
    with pytest.raises(ValueError):
        _pc(birth_date="1985-03-12")


def test_gender_must_be_m_f_o():
    with pytest.raises(ValueError):
        _pc(gender="female")


def test_exclude_ids_is_frozen():
    p = _pc(exclude_ids={"a"})
    assert type(p.exclude_ids) is frozenset
    assert p.exclude_ids == frozenset({"a"})


@pytest.mark.asyncio
async def test_default_create_patient_is_write_not_supported():
    adapter = GenericRestAdapter({"base_url": "https://x", "list_appointments_path": "/a"})
    with pytest.raises(WriteNotSupported):
        await adapter.create_patient(_pc())
