"""Roster identity: the name, identifiers and active flag an admin maps doctors by (v0.13.0)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.exceptions import TransientError

NPI = "http://hl7.org/fhir/sid/us-npi"


def _adapter(resources: list[dict]) -> FhirR4Adapter:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": r} for r in resources],
            },
        )

    a = FhirR4Adapter(
        base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"}
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _prac(**fields) -> dict:
    return {"resourceType": "Practitioner", "id": fields.pop("id", "p-1"), **fields}


async def _one(resource: dict):
    roster = await _adapter([resource]).fetch_doctor_roster(as_of_date=date(2026, 9, 24))
    assert len(roster) == 1
    return roster[0]


@pytest.mark.asyncio
async def test_name_text_is_used_verbatim():
    doc = await _one(_prac(name=[{"use": "official", "text": "Dr. Asha Rao"}]))
    assert doc.display_name == "Dr. Asha Rao"


@pytest.mark.asyncio
async def test_official_or_usual_name_wins_over_an_earlier_other_use():
    doc = await _one(
        _prac(
            name=[
                {"use": "nickname", "text": "Ash"},
                {"use": "usual", "text": "Asha Rao"},
                {"use": "official", "text": "Asha K Rao"},
            ]
        )
    )
    assert doc.display_name == "Asha Rao", "the FIRST official-or-usual entry, in order"


@pytest.mark.asyncio
async def test_old_names_are_skipped_when_nothing_is_official():
    doc = await _one(_prac(name=[{"use": "old", "text": "Asha Menon"}, {"text": "Asha Rao"}]))
    assert doc.display_name == "Asha Rao"


@pytest.mark.asyncio
async def test_name_falls_back_to_prefix_given_family():
    doc = await _one(
        _prac(
            name=[{"use": "official", "prefix": ["Dr."], "given": ["Asha", "K"], "family": "Rao"}]
        )
    )
    assert doc.display_name == "Dr. Asha K Rao"


@pytest.mark.asyncio
async def test_no_usable_name_is_none():
    assert (await _one(_prac(name=[{"use": "old", "text": "Gone"}]))).display_name is None
    assert (await _one(_prac())).display_name is None


@pytest.mark.asyncio
async def test_identifiers_keep_system_and_drop_empty_values():
    doc = await _one(
        _prac(
            identifier=[
                {"system": NPI, "value": "1234567893"},
                {"value": "KMC-45678"},
                {"system": "urn:x", "value": ""},
                {"system": "urn:y"},
            ]
        )
    )
    assert doc.identifiers == [(NPI, "1234567893"), ("", "KMC-45678")]


@pytest.mark.asyncio
async def test_active_is_read_when_present_and_none_when_absent():
    assert (await _one(_prac(active=False))).active is False
    assert (await _one(_prac(active=True))).active is True
    assert (await _one(_prac())).active is None


@pytest.mark.asyncio
async def test_fetch_doctor_roster_failed_page_raises():
    """Page 1 fine, page 2 fails: a partial roster would silently make every
    practitioner on page 2 look deleted."""
    page1 = {
        "resourceType": "Bundle",
        "link": [{"relation": "next", "url": "https://hms.example/fhir/Practitioner?page=2"}],
        "entry": [{"resource": _prac(id="p-1")}],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if "page=2" in str(req.url):
            return httpx.Response(502)
        return httpx.Response(200, json=page1)

    a = FhirR4Adapter(
        base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"}
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TransientError):
        await a.fetch_doctor_roster(as_of_date=date(2026, 9, 24))


@pytest.mark.asyncio
async def test_fetch_doctor_roster_non_json_page_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    a = FhirR4Adapter(
        base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"}
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TransientError):
        await a.fetch_doctor_roster(as_of_date=date(2026, 9, 24))
