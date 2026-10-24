import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.exceptions import AuthError, VendorRejected
from sm_common.phone import hash_phone_for_lookup

FIX = Path(__file__).parent.parent / "fixtures" / "bahmni"
BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
SEARCH = f"{REST}/bahmni/search/patient"
SALT = "salt"
UUID_1 = "11111111-aaaa-4aaa-8aaa-000000000001"
UUID_2 = "22222222-bbbb-4bbb-8bbb-000000000002"


def _fx(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((FIX / name).read_text())


def _adapter(**cfg: object) -> BahmniAdapter:
    config = BahmniConfig(
        location_uuid="L",
        identifier_type_uuid="T",
        identifier_source_uuid="S",
        **cfg,  # type: ignore[arg-type]
    )
    return BahmniAdapter(
        http=BahmniHttp(BASE, "u", "p"), config=config, hash_salt=SALT, transport_key="k"
    )


def _login() -> respx.Route:
    return respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


@respx.mock
async def test_search_by_phone_returns_all_household_hits():
    _login()
    route = respx.get(SEARCH).mock(
        return_value=httpx.Response(200, json=_fx("search_patient_two_hits.json"))
    )
    hits = await _adapter().search_patients(phone="9876543210")
    assert [h.resource_id for h in hits] == [UUID_1, UUID_2]  # deduped across phone variants
    assert route.call_count >= 2  # one query per stored-shape variant
    params = route.calls[0].request.url.params
    assert params["customAttribute"] == "+919876543210"
    assert params["patientAttributes"] == "phoneNumber"
    assert params["s"] == "byIdOrNameOrVillage"
    assert params["q"] == ""
    first = hits[0]
    assert first.phone_hash == hash_phone_for_lookup("9876543210", SALT)
    assert first.name_token == "Asha Verma"
    assert first.gender == "F"
    assert first.birth_date == date(1990, 5, 15)  # IST-midnight epoch is still the 15th
    assert first.birth_date_precision == "exact"


@respx.mock
async def test_find_patient_by_phone_ambiguous_returns_none():
    _login()
    respx.get(SEARCH).mock(
        return_value=httpx.Response(200, json=_fx("search_patient_two_hits.json"))
    )
    assert await _adapter().find_patient(phone="9876543210") is None


@respx.mock
async def test_find_patient_single_hit_and_zero_hits():
    _login()
    one = _fx("search_patient_two_hits.json")
    one["pageOfResults"] = one["pageOfResults"][:1]
    one["totalCount"] = 1
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json=one))
    found = await _adapter().find_patient(phone="9876543210")
    assert found is not None and found.resource_id == UUID_1
    route.mock(return_value=httpx.Response(200, json={"pageOfResults": [], "totalCount": 0}))
    assert await _adapter().find_patient(phone="9876543210") is None


@respx.mock
async def test_find_by_mrn_fills_mrn_and_resource_id():
    _login()
    one = _fx("search_patient_two_hits.json")
    one["pageOfResults"] = one["pageOfResults"][:1]
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json=one))
    found = await _adapter().find_patient(mrn="GAN200001")
    assert found is not None
    assert found.mrn == "GAN200001"
    assert found.resource_id == UUID_1
    assert route.calls[0].request.url.params["identifier"] == "GAN200001"


@respx.mock
async def test_phone_hash_only_returns_empty_without_http():
    session = _login()
    search = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={}))
    adapter = _adapter()
    assert await adapter.search_patients(phone_hash="deadbeef") == []
    assert await adapter.find_patient(phone_hash="deadbeef") is None
    assert not session.called
    assert not search.called


@respx.mock
async def test_name_dob_search_filters_exact_dob():
    _login()
    route = respx.get(SEARCH).mock(
        return_value=httpx.Response(200, json=_fx("search_patient_two_hits.json"))
    )
    hits = await _adapter().search_patients(family="Verma", birth_date=date(1990, 5, 15))
    assert [h.resource_id for h in hits] == [UUID_1]
    assert route.calls[0].request.url.params["q"] == "Verma"
    assert await _adapter().search_patients(family="Verma", birth_date=date(1990, 5, 16)) == []


async def test_family_without_birth_date_is_refused_not_empty():
    with pytest.raises(ValueError):
        await _adapter().search_patients(family="Verma")


@respx.mock
async def test_search_follows_pages_until_total():
    _login()
    full = _fx("search_patient_two_hits.json")
    p1 = {"totalCount": 2, "pageOfResults": full["pageOfResults"][:1]}
    p2 = {"totalCount": 2, "pageOfResults": full["pageOfResults"][1:]}
    route = respx.get(SEARCH).mock(
        side_effect=[httpx.Response(200, json=p1), httpx.Response(200, json=p2)]
    )
    hits = await _adapter().search_patients(mrn="GAN")
    assert [h.resource_id for h in hits] == [UUID_1, UUID_2]
    assert [c.request.url.params["startIndex"] for c in route.calls] == ["0", "1"]


@respx.mock
async def test_get_patient_full_profile():
    _login()
    route = respx.get(f"{REST}/patientprofile/{UUID_1}").mock(
        return_value=httpx.Response(200, json=_fx("patient_full.json"))
    )
    p = await _adapter().get_patient(UUID_1)
    assert p is not None
    assert route.calls[0].request.url.params["v"] == "full"
    assert (p.resource_id, p.mrn, p.name_token, p.gender) == (
        UUID_1,
        "GAN200001",
        "Asha Verma",
        "F",
    )
    assert p.birth_date == date(1990, 5, 15)
    assert p.phone_hash == hash_phone_for_lookup("9876543210", SALT)


@respx.mock
async def test_estimated_birthdate_is_year_precision():
    _login()
    body = _fx("patient_full.json")
    body["patient"]["person"]["birthdateEstimated"] = True
    respx.get(f"{REST}/patientprofile/{UUID_1}").mock(return_value=httpx.Response(200, json=body))
    p = await _adapter().get_patient(UUID_1)
    assert p is not None and p.birth_date_precision == "year"


@respx.mock
async def test_get_patient_404_none():
    _login()
    respx.get(f"{REST}/patientprofile/nope").mock(return_value=httpx.Response(404))
    assert await _adapter().get_patient("nope") is None


@respx.mock
async def test_roster_drops_system_providers():
    _login()
    respx.get(f"{REST}/provider").mock(
        return_value=httpx.Response(200, json=_fx("provider_full.json"))
    )
    docs = await _adapter().fetch_doctor_roster(date(2026, 10, 25))
    assert [d.external_doctor_id for d in docs] == [
        "p-doc-1",
        "p-doc-2",
    ]  # no retired/no-person/UNKNOWN/LABMANAGER
    d = docs[0]
    assert d.display_name == "Dr Meera Iyer"
    assert d.identifiers == [("", "DOC001")]
    assert d.active is True
    assert d.speciality_label == ""


@respx.mock
async def test_roster_speciality_from_mapped_service():
    _login()
    respx.get(f"{REST}/provider").mock(
        return_value=httpx.Response(200, json=_fx("provider_full.json"))
    )
    route = respx.get(f"{REST}/appointmentService/all/default").mock(
        return_value=httpx.Response(200, json=[{"uuid": "svc-1", "name": "Cardiology"}])
    )
    docs = await _adapter(service_by_practitioner={"p-doc-1": "svc-1"}).fetch_doctor_roster(
        date(2026, 10, 25)
    )
    assert route.called
    assert [d.speciality_label for d in docs] == ["Cardiology", ""]


@respx.mock
async def test_roster_follows_next_link():
    _login()
    page1 = {"results": _fx("provider_full.json")["results"][:1], "links": [{"rel": "next"}]}
    page2 = {"results": _fx("provider_full.json")["results"][1:2]}
    route = respx.get(f"{REST}/provider").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    docs = await _adapter().fetch_doctor_roster(date(2026, 10, 25))
    assert [d.external_doctor_id for d in docs] == ["p-doc-1", "p-doc-2"]
    assert [c.request.url.params["startIndex"] for c in route.calls] == ["0", "100"]


@respx.mock
async def test_auth_failure_surfaces():
    respx.get(f"{REST}/session").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await _adapter().find_patient(mrn="X")


@respx.mock
async def test_400_on_search_is_vendor_rejected():
    _login()
    respx.get(SEARCH).mock(return_value=httpx.Response(400, json={"error": {"message": "bad q"}}))
    with pytest.raises(VendorRejected, match="bad q"):
        await _adapter().find_patient(mrn="X")
