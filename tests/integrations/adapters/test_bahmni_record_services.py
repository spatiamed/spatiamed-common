import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import NotFound, VendorRejected
from sm_common.integrations.record_types import RecordService

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
SERVICES = f"{REST}/appointmentService/all/default"
ALLERGY = f"{BASE}/openmrs/ws/fhir2/R4/AllergyIntolerance"


def _client() -> BahmniRecordClient:
    cfg = BahmniConfig(
        location_uuid="loc-1", identifier_type_uuid="idtype", identifier_source_uuid="idsrc"
    )
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), cfg)


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _bundle(*names: str | None, nxt: bool = False) -> dict:
    entries = []
    for n in names:
        res: dict = {"resourceType": "AllergyIntolerance"}
        if n is not None:
            res["code"] = {"text": n}
        entries.append({"resource": res})
    body: dict = {"resourceType": "Bundle", "entry": entries}
    if nxt:
        body["link"] = [{"relation": "next", "url": "x"}]
    return body


@respx.mock
async def test_list_services() -> None:
    _login()
    respx.get(SERVICES).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"uuid": "s1", "name": "General OPD", "durationMins": 15},
                {"uuid": "s2", "name": "Dental"},
            ],
        )
    )
    assert await _client().list_services() == [
        RecordService("s1", "General OPD", 15),
        RecordService("s2", "Dental", None),
    ]


@respx.mock
async def test_list_services_malformed_is_loud() -> None:
    _login()
    respx.get(SERVICES).mock(return_value=httpx.Response(200, json={"oops": 1}))
    with pytest.raises(VendorRejected):
        await _client().list_services()
    respx.get(SERVICES).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().list_services()


@respx.mock
async def test_allergies_404_returns_none() -> None:
    _login()
    respx.get(ALLERGY).mock(return_value=httpx.Response(404))
    assert await _client().allergies("p-1") is None


@respx.mock
async def test_allergies_names_and_query() -> None:
    _login()
    route = respx.get(ALLERGY).mock(
        return_value=httpx.Response(200, json=_bundle("Penicillin", None))
    )
    out = await _client().allergies("p-1")
    assert out == ["Penicillin", "Unspecified allergen"]
    assert route.calls[0].request.url.params["patient"] == "p-1"


@respx.mock
async def test_allergies_empty_is_empty_list_not_none() -> None:
    _login()
    respx.get(ALLERGY).mock(return_value=httpx.Response(200, json=_bundle()))
    assert await _client().allergies("p-1") == []


@respx.mock
async def test_allergies_truncated_or_malformed_is_loud() -> None:
    _login()
    respx.get(ALLERGY).mock(return_value=httpx.Response(200, json=_bundle("A", nxt=True)))
    with pytest.raises(VendorRejected):
        await _client().allergies("p-1")
    respx.get(ALLERGY).mock(return_value=httpx.Response(200, json={"resourceType": "Other"}))
    with pytest.raises(VendorRejected):
        await _client().allergies("p-1")
