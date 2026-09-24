"""bahmni / generic_rest / mocdoc construct CanonicalDoctor without the v0.13.0
identity fields; they must keep building, with the fields defaulted."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.adapters.mocdoc import MocDocAdapter
from tests.integrations.test_generic_rest_adapter import MEDIXCEL_MAPPING

AS_OF = date(2026, 9, 24)


def _assert_defaulted(doc) -> None:
    assert doc.display_name is None
    assert doc.identifiers == []
    assert doc.active is None


@pytest.mark.asyncio
@respx.mock
async def test_bahmni_roster_builds_with_defaults():
    respx.get("https://bahmni.example.com/openmrs/ws/rest/v1/provider").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "uuid": "prov-1",
                        "attributes": [
                            {"attributeType": {"display": "speciality"}, "value": "Cardiology"}
                        ],
                    }
                ]
            },
        )
    )
    a = BahmniAdapter(
        base_url="https://bahmni.example.com",
        auth_scheme="api_key",
        api_key="k",
        hash_salt="s",
        transport_key="test-transport-key-32-char-padding!",
    )
    [doc] = await a.fetch_doctor_roster(AS_OF)
    assert doc.external_doctor_id == "prov-1"
    _assert_defaulted(doc)


@pytest.mark.asyncio
@respx.mock
async def test_generic_rest_roster_builds_with_defaults():
    respx.get("https://api.medixcel.in/v2/doctors").mock(
        return_value=httpx.Response(200, json={"doctors": [{"docId": "D1", "consultDuration": 20}]})
    )
    [doc] = await GenericRestAdapter(MEDIXCEL_MAPPING).fetch_doctor_roster(AS_OF)
    assert doc.external_doctor_id == "D1"
    _assert_defaulted(doc)


@pytest.mark.asyncio
@respx.mock
async def test_mocdoc_roster_builds_with_defaults():
    respx.get("https://api.mocdoc.in/api/v1/doctors").mock(
        return_value=httpx.Response(200, json={"doctors": [{"id": "M1", "durationMinutes": 15}]})
    )
    a = MocDocAdapter(
        base_url="https://api.mocdoc.in",
        api_key="k",
        api_secret="s",
        hash_salt="salt",
        transport_key="test-transport-32-padding-chars!!",
    )
    [doc] = await a.fetch_doctor_roster(AS_OF)
    assert doc.external_doctor_id == "M1"
    _assert_defaulted(doc)
