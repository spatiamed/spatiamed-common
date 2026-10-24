import json
from datetime import date
from uuid import UUID

import httpx
import pytest
import respx

from sm_common.integrations import PatientCreate, PatientCreateResult
from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.build_adapter import AdapterBuildConfig, build_bahmni_record_client
from sm_common.integrations.exceptions import ConfigError, ConflictError, VendorRejected
from sm_common.integrations.record_types import (
    RecordAddress,
    RecordPatientInput,
    RecordPatientPatch,
)

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
SEARCH = f"{REST}/bahmni/search/patient"
CREATE = f"{REST}/bahmnicore/patientprofile"
MARKER = UUID("0b1c2d3e-0000-4000-8000-00000000abcd")
MARKER_VALUE = f"sm:{MARKER}"


def _config(**over: object) -> BahmniConfig:
    base: dict[str, object] = {
        "location_uuid": "L",
        "identifier_type_uuid": "idtype",
        "identifier_source_uuid": "idsrc",
        "phone_attribute_type_uuid": "attr-phone",
    }
    base.update(over)
    return BahmniConfig(**base)  # type: ignore[arg-type]


MARKER_CFG = {"marker_attribute_name": "smMarker", "marker_attribute_type_uuid": "attr-marker"}


def _client(**over: object) -> BahmniRecordClient:
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), _config(**over))


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _input(**over: object) -> RecordPatientInput:
    base: dict[str, object] = {
        "marker": MARKER,
        "given": "Asha",
        "family": "Verma",
        "gender": "F",
        "birth_date": date(1990, 5, 15),
        "birth_date_estimated": False,
        "phone": "9876543210",
        "address": RecordAddress(
            line1="12 Lane", city_village="Pune", district="Pune", state="MH", pincode="411001"
        ),
    }
    base.update(over)
    return RecordPatientInput(**base)  # type: ignore[arg-type]


CREATED = {"patient": {"uuid": "u-new", "identifiers": [{"identifier": "GAN200009"}]}}


def _hit(uuid: str, marker: str | None = None, name: str = "smMarker") -> dict:  # type: ignore[type-arg]
    attrs = {name: marker} if marker is not None else {}
    return {
        "uuid": uuid,
        "identifier": "GAN200001",
        "givenName": "Asha",
        "familyName": "Verma",
        "gender": "F",
        "birthDate": 642709800000,
        "age": 36,
        "customAttribute": json.dumps(attrs),
        "addressFieldValue": "Pune",
    }


@respx.mock
async def test_create_sends_idgen_identifier_and_phone_attr():
    _login()
    search = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"pageOfResults": []}))
    post = respx.post(CREATE).mock(return_value=httpx.Response(200, json=CREATED))
    patient, created = await _client().create_patient(_input())
    body = json.loads(post.calls[0].request.content)
    assert body["patient"]["identifiers"] == [
        {
            "identifierType": "idtype",
            "identifierSourceUuid": "idsrc",
            "preferred": True,
            "voided": False,
        }
    ]
    person = body["patient"]["person"]
    assert person["names"] == [{"givenName": "Asha", "familyName": "Verma", "preferred": True}]
    assert person["gender"] == "F"
    assert person["birthdate"] == "1990-05-15"
    assert person["birthdateEstimated"] is False
    assert person["attributes"] == [
        {"attributeType": {"uuid": "attr-phone"}, "value": "9876543210"}
    ]
    assert person["addresses"] == [
        {
            "address1": "12 Lane",
            "cityVillage": "Pune",
            "countyDistrict": "Pune",
            "stateProvince": "MH",
            "postalCode": "411001",
        }
    ]
    assert (patient.uuid, patient.identifier, created) == ("u-new", "GAN200009", True)
    assert patient.phone == "9876543210" and patient.birth_date == date(1990, 5, 15)
    # Household phones are legal: no phone pre-search, no marker configured -> no search at all.
    assert not search.called


@respx.mock
async def test_create_allows_phone_already_held_by_another_patient():
    _login()
    respx.get(SEARCH).mock(
        return_value=httpx.Response(
            200, json={"pageOfResults": [_hit("other", None)], "totalCount": 1}
        )
    )
    post = respx.post(CREATE).mock(return_value=httpx.Response(200, json=CREATED))
    _, created = await _client(**MARKER_CFG).create_patient(_input())
    assert created is True and post.call_count == 1


@respx.mock
async def test_create_with_marker_sends_marker_attribute():
    _login()
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"pageOfResults": []}))
    post = respx.post(CREATE).mock(return_value=httpx.Response(200, json=CREATED))
    await _client(**MARKER_CFG).create_patient(_input())
    attrs = json.loads(post.calls[0].request.content)["patient"]["person"]["attributes"]
    assert {"attributeType": {"uuid": "attr-marker"}, "value": MARKER_VALUE} in attrs


@respx.mock
async def test_create_with_marker_returns_existing():
    _login()
    search = respx.get(SEARCH).mock(
        return_value=httpx.Response(
            200, json={"pageOfResults": [_hit("u-old", MARKER_VALUE)], "totalCount": 1}
        )
    )
    post = respx.post(CREATE).mock(return_value=httpx.Response(200, json=CREATED))
    patient, created = await _client(**MARKER_CFG).create_patient(_input())
    assert (patient.uuid, created) == ("u-old", False)
    assert not post.called
    params = search.calls[0].request.url.params
    assert params["customAttribute"] == MARKER_VALUE
    assert params["patientAttributes"] == "smMarker"


@respx.mock
async def test_create_marker_search_ignores_loose_matches():
    _login()
    respx.get(SEARCH).mock(
        return_value=httpx.Response(
            200, json={"pageOfResults": [_hit("x", MARKER_VALUE + "-other")], "totalCount": 1}
        )
    )
    post = respx.post(CREATE).mock(return_value=httpx.Response(200, json=CREATED))
    _, created = await _client(**MARKER_CFG).create_patient(_input())
    assert created is True and post.called


@respx.mock
async def test_create_marker_two_hits_conflict():
    _login()
    respx.get(SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "pageOfResults": [_hit("a", MARKER_VALUE), _hit("b", MARKER_VALUE)],
                "totalCount": 2,
            },
        )
    )
    post = respx.post(CREATE).mock(return_value=httpx.Response(200, json=CREATED))
    with pytest.raises(ConflictError) as ei:
        await _client(**MARKER_CFG).create_patient(_input())
    assert ei.value.landed is True
    assert not post.called


@respx.mock
async def test_create_400_vendor_rejected():
    _login()
    respx.post(CREATE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "Identifier source is empty"}})
    )
    with pytest.raises(VendorRejected, match="Identifier source"):
        await _client().create_patient(_input())


@respx.mock
async def test_create_response_without_uuid_is_landed_rejection():
    _login()
    respx.post(CREATE).mock(return_value=httpx.Response(200, json={"patient": {}}))
    with pytest.raises(VendorRejected) as ei:
        await _client().create_patient(_input())
    assert ei.value.landed is True


async def test_create_config_gaps_fail_loudly():
    with pytest.raises(ConfigError, match="phone_attribute_type_uuid"):
        await _client(phone_attribute_type_uuid="").create_patient(_input())
    with pytest.raises(ConfigError, match="marker"):
        await _client(marker_attribute_name="smMarker").create_patient(_input())


PROFILE = {
    "patient": {
        "uuid": "u1",
        "identifiers": [{"identifier": "GAN200001", "preferred": True}],
        "person": {
            "uuid": "u1",
            "gender": "F",
            "birthdate": "1990-05-15T00:00:00.000+0530",
            "birthdateEstimated": False,
            "dead": False,
            "names": [{"uuid": "name-1", "givenName": "Asha", "familyName": "Verma"}],
            "addresses": [{"uuid": "addr-1", "cityVillage": "Pune", "postalCode": "411001"}],
            "attributes": [
                {
                    "uuid": "pa-1",
                    "attributeType": {"uuid": "attr-phone", "display": "phoneNumber"},
                    "value": "9876543210",
                }
            ],
        },
    }
}


@respx.mock
async def test_update_sends_only_changed_fields():
    _login()
    respx.get(f"{REST}/patientprofile/u1").mock(return_value=httpx.Response(200, json=PROFILE))
    post = respx.post(f"{CREATE}/u1").mock(return_value=httpx.Response(200, json=PROFILE))
    patient = await _client().update_patient("u1", RecordPatientPatch(given="Anita", phone="111"))
    person = json.loads(post.calls[0].request.content)["patient"]["person"]
    assert set(person) == {"uuid", "names", "attributes"}
    assert person["names"] == [{"uuid": "name-1", "givenName": "Anita", "familyName": "Verma"}]
    assert person["attributes"] == [
        {"uuid": "pa-1", "attributeType": {"uuid": "attr-phone"}, "value": "111"}
    ]
    assert (patient.given, patient.family, patient.phone) == ("Anita", "Verma", "111")
    assert patient.birth_date == date(1990, 5, 15)


@respx.mock
async def test_update_demographics_fields_and_address():
    _login()
    respx.get(f"{REST}/patientprofile/u1").mock(return_value=httpx.Response(200, json=PROFILE))
    post = respx.post(f"{CREATE}/u1").mock(return_value=httpx.Response(200, json=PROFILE))
    patch = RecordPatientPatch(
        gender="M",
        birth_date=date(1991, 1, 2),
        birth_date_estimated=True,
        address=RecordAddress(city_village="Nashik"),
    )
    patient = await _client().update_patient("u1", patch)
    person = json.loads(post.calls[0].request.content)["patient"]["person"]
    assert set(person) == {"uuid", "gender", "birthdate", "birthdateEstimated", "addresses"}
    assert person["birthdate"] == "1991-01-02" and person["birthdateEstimated"] is True
    assert person["addresses"][0]["uuid"] == "addr-1"
    assert person["addresses"][0]["cityVillage"] == "Nashik"
    assert patient.gender == "M" and patient.address.city_village == "Nashik"


@respx.mock
async def test_update_empty_patch_makes_no_write():
    _login()
    respx.get(f"{REST}/patientprofile/u1").mock(return_value=httpx.Response(200, json=PROFILE))
    post = respx.post(f"{CREATE}/u1").mock(return_value=httpx.Response(200, json=PROFILE))
    patient = await _client().update_patient("u1", RecordPatientPatch())
    assert not post.called and patient.uuid == "u1"


@respx.mock
async def test_update_unknown_patient_rejected():
    _login()
    respx.get(f"{REST}/patientprofile/zz").mock(return_value=httpx.Response(404))
    with pytest.raises(VendorRejected, match="not found"):
        await _client().update_patient("zz", RecordPatientPatch(given="X"))


@respx.mock
async def test_get_patient_and_404():
    _login()
    respx.get(f"{REST}/patientprofile/u1").mock(return_value=httpx.Response(200, json=PROFILE))
    respx.get(f"{REST}/patientprofile/nope").mock(return_value=httpx.Response(404))
    p = await _client().get_patient("u1")
    assert p is not None
    assert (p.identifier, p.given, p.phone, p.dead) == ("GAN200001", "Asha", "9876543210", False)
    assert p.address == RecordAddress(city_village="Pune", pincode="411001")
    assert await _client().get_patient("nope") is None


@respx.mock
async def test_search_truncated_flag():
    _login()
    page = {"totalCount": 40, "pageOfResults": [_hit(f"u{i}") for i in range(10)]}
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json=page))
    hits, truncated = await _client().search(phone="9876543210")
    assert len(hits) == 10 and truncated is True
    params = route.calls[0].request.url.params
    assert params["customAttribute"] == "9876543210"
    assert params["patientAttributes"] == "phoneNumber"
    assert params["s"] == "byIdOrNameOrVillage"
    respx.get(SEARCH).mock(
        return_value=httpx.Response(200, json={"totalCount": 1, "pageOfResults": [_hit("u1")]})
    )
    hits, truncated = await _client().search(identifier="GAN200001")
    assert len(hits) == 1 and truncated is False


@respx.mock
async def test_adapter_create_patient_delegates():
    _login()
    post = respx.post(CREATE).mock(return_value=httpx.Response(200, json=CREATED))
    adapter = BahmniAdapter(
        http=BahmniHttp(BASE, "u", "p"), config=_config(), hash_salt="s", transport_key="k"
    )
    result = await adapter.create_patient(
        PatientCreate(
            patient_marker=MARKER,
            family="Verma",
            given=["Asha", "Rani"],
            birth_date=date(1990, 5, 15),
            gender="F",
            phone="9876543210",
        )
    )
    assert result == PatientCreateResult(resource_id="u-new", mrn="GAN200009", created=True)
    person = json.loads(post.calls[0].request.content)["patient"]["person"]
    assert person["names"][0]["givenName"] == "Asha Rani"
    assert person["birthdateEstimated"] is False


def test_build_bahmni_record_client():
    cfg = AdapterBuildConfig(
        vendor="bahmni",
        base_url=BASE,
        credentials={"username": "u", "password": "p"},
        field_mapping={
            "location_uuid": "L",
            "identifier_type_uuid": "T",
            "identifier_source_uuid": "S",
        },
    )
    assert isinstance(build_bahmni_record_client(cfg), BahmniRecordClient)
