import json
from pathlib import Path

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import (
    AuthError,
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.record_types import RecordBed, RecordBedAssignment, RecordWard

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
WARDS = f"{REST}/admissionLocation"
BEDS = f"{REST}/beds"
LOCATION = f"{REST}/location"
FIX = Path(__file__).parent.parent / "fixtures" / "bahmni"
REQ = {"location_uuid": "L", "identifier_type_uuid": "T", "identifier_source_uuid": "S"}

GENERAL = "08ed3779-ac52-4fa4-8e93-686892cf4527"
PRIVATE = "812a14eb-818f-4f89-8e24-087ed39e2fd7"
PATIENT = "849e3827-09bb-4123-a07b-3fd63801d0d5"


def _fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def _client() -> BahmniRecordClient:
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig.from_mapping(REQ))


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def test_config_new_keys_default_empty() -> None:
    cfg = BahmniConfig.from_mapping(REQ)
    assert cfg.visit_location_uuid == ""
    assert cfg.ipd_visit_type_uuid == ""
    assert cfg.admission_encounter_type_uuid == ""
    assert cfg.transfer_encounter_type_uuid == ""
    assert cfg.discharge_encounter_type_uuid == ""
    assert cfg.discharge_summary_encounter_type_uuid == ""
    assert cfg.admit_new_ipd_visit is True
    assert cfg.disposition_codes == {}

    set_ = BahmniConfig.from_mapping(
        {
            **REQ,
            "visit_location_uuid": "VL",
            "admit_new_ipd_visit": False,
            "disposition_codes": {"admit": "Admit Patient", "discharge": "Discharge Patient"},
        }
    )
    assert set_.visit_location_uuid == "VL"
    assert set_.admit_new_ipd_visit is False
    assert set_.disposition_codes == {"admit": "Admit Patient", "discharge": "Discharge Patient"}


def test_config_admit_new_ipd_visit_must_be_bool() -> None:
    # A JSON "false" is truthy; it must not silently mean True.
    with pytest.raises(ConfigError, match="admit_new_ipd_visit"):
        BahmniConfig.from_mapping({**REQ, "admit_new_ipd_visit": "false"})


@pytest.mark.parametrize(
    "key", ["disposition_codes", "concept_map", "service_by_practitioner", "document_type_concepts"]
)
def test_config_mapping_keys_must_be_mappings(key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        BahmniConfig.from_mapping({**REQ, key: "Admit Patient"})


@respx.mock
async def test_list_wards_maps_counts() -> None:
    _login()
    route = respx.get(WARDS).mock(
        return_value=httpx.Response(200, json=_fixture("admission_locations.json"))
    )
    wards = await _client().list_wards()
    assert wards == [
        RecordWard(uuid=GENERAL, name="General Ward", total_beds=2, occupied_beds=1, beds=()),
        RecordWard(uuid=PRIVATE, name="Private Ward", total_beds=0, occupied_beds=0, beds=()),
    ]
    assert route.call_count == 1
    assert "v" not in route.calls[0].request.url.params


@respx.mock
async def test_list_wards_follows_next_page() -> None:
    _login()
    page1 = _fixture("admission_locations.json")
    page1["results"] = page1["results"][:1]
    page1["links"] = [{"rel": "next", "uri": f"{WARDS}?startIndex=1"}]
    page2 = _fixture("admission_locations.json")
    page2["results"] = page2["results"][1:]
    route = respx.get(WARDS).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    wards = await _client().list_wards()
    assert [w.uuid for w in wards] == [GENERAL, PRIVATE]
    assert route.calls[1].request.url.params["startIndex"] == "1"


@respx.mock
async def test_list_wards_404_and_malformed_are_loud() -> None:
    _login()
    # 404 here means the bed-management module is absent, never "no wards".
    respx.get(WARDS).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().list_wards()
    respx.get(WARDS).mock(return_value=httpx.Response(200, json={"results": [{"ward": {}}]}))
    with pytest.raises(VendorRejected):
        await _client().list_wards()


@respx.mock
async def test_get_ward_maps_bed_layout_and_patients() -> None:
    _login()
    route = respx.get(f"{WARDS}/{GENERAL}").mock(
        return_value=httpx.Response(200, json=_fixture("admission_location_full.json"))
    )
    ward = await _client().get_ward(GENERAL)
    assert route.calls[0].request.url.params["v"] == "full"
    assert ward == RecordWard(
        uuid=GENERAL,
        name="General Ward",
        total_beds=2,
        occupied_beds=1,
        # The empty layout cell (no bed mapped) is not a bed.
        beds=(
            RecordBed(
                bed_id=11,
                bed_uuid="560544a8-97e7-4b10-b84a-f72dbac4561e",
                bed_number="GW-11",
                bed_type="general",
                status="OCCUPIED",
                row=1,
                column=1,
                patient_uuids=(PATIENT,),
            ),
            RecordBed(
                bed_id=12,
                bed_uuid="46dc5ad0-8385-4426-8e3b-350a4b0c9ad8",
                bed_number="GW-12",
                bed_type="general",
                status="AVAILABLE",
                row=1,
                column=2,
                patient_uuids=(),
            ),
        ),
    )


@respx.mock
async def test_get_ward_route_404_not_found() -> None:
    # Bahmni never answers 404 for a missing ward, so a 404 is a missing route.
    _login()
    respx.get(f"{WARDS}/{PRIVATE}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().get_ward(PRIVATE)


@respx.mock
async def test_get_ward_unknown_uuid_500_then_location_404_is_none() -> None:
    # Bahmni answers 500 for an unknown ward uuid; the core location route says 404.
    _login()
    respx.get(f"{WARDS}/{PRIVATE}").mock(return_value=httpx.Response(500))
    probe = respx.get(f"{LOCATION}/{PRIVATE}").mock(return_value=httpx.Response(404))
    assert await _client().get_ward(PRIVATE) is None
    assert probe.call_count == 1


@respx.mock
async def test_get_ward_500_with_location_present_stays_transient() -> None:
    _login()
    respx.get(f"{WARDS}/{PRIVATE}").mock(return_value=httpx.Response(500))
    probe = respx.get(f"{LOCATION}/{PRIVATE}").mock(
        return_value=httpx.Response(200, json={"uuid": PRIVATE})
    )
    with pytest.raises(TransientError, match="HTTP 500"):
        await _client().get_ward(PRIVATE)
    assert probe.call_count == 1


@respx.mock
async def test_get_ward_500_probe_500_stays_transient() -> None:
    _login()
    respx.get(f"{WARDS}/{PRIVATE}").mock(return_value=httpx.Response(500))
    respx.get(f"{LOCATION}/{PRIVATE}").mock(return_value=httpx.Response(503))
    with pytest.raises(TransientError, match="HTTP 500"):
        await _client().get_ward(PRIVATE)


@respx.mock
async def test_get_ward_auth_error_not_probed() -> None:
    _login()
    respx.get(f"{WARDS}/{PRIVATE}").mock(return_value=httpx.Response(403))
    probe = respx.get(f"{LOCATION}/{PRIVATE}").mock(return_value=httpx.Response(404))
    with pytest.raises(AuthError):
        await _client().get_ward(PRIVATE)
    assert probe.call_count == 0


@pytest.mark.parametrize("bad", ["", "a/b", "x?v=full", "../session", "a" * 39])
async def test_get_ward_rejects_malformed_uuid_before_http(bad: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="ward_uuid"):
            await _client().get_ward(bad)
        assert mock.calls.call_count == 0


@respx.mock
async def test_list_wards_page_cap_is_loud() -> None:
    _login()
    page = _fixture("admission_locations.json")
    page["results"] = page["results"][:1]
    page["links"] = [{"rel": "next", "uri": f"{WARDS}?startIndex=1"}]
    route = respx.get(WARDS).mock(return_value=httpx.Response(200, json=page))
    with pytest.raises(VendorRejected, match="20 pages"):
        await _client().list_wards()
    assert route.call_count == 20


@respx.mock
async def test_current_bed_none_when_unassigned() -> None:
    _login()
    route = respx.get(BEDS).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await _client().current_bed(PATIENT) is None
    assert route.calls[0].request.url.params["patientUuid"] == PATIENT


@respx.mock
async def test_current_bed_route_404_not_found() -> None:
    # Never "unassigned": that answer would unlock a second bed.
    _login()
    respx.get(BEDS).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().current_bed(PATIENT)


@respx.mock
async def test_current_bed_two_rows_is_loud() -> None:
    _login()
    body = _fixture("beds_by_patient.json")
    body["results"] = body["results"] * 2
    respx.get(BEDS).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(VendorRejected):
        await _client().current_bed(PATIENT)


@respx.mock
async def test_current_bed_enriches_from_ward_layout() -> None:
    # beds?patientUuid= carries only bedId/bedNumber/ward: Bahmni builds that bed
    # from two columns, so bedType is null and there is no bedUuid. The ward layout has both.
    _login()
    respx.get(BEDS).mock(return_value=httpx.Response(200, json=_fixture("beds_by_patient.json")))
    ward = respx.get(f"{WARDS}/{GENERAL}").mock(
        return_value=httpx.Response(200, json=_fixture("admission_location_full.json"))
    )
    assert await _client().current_bed(PATIENT) == RecordBedAssignment(
        bed_id=11,
        bed_uuid="560544a8-97e7-4b10-b84a-f72dbac4561e",
        bed_number="GW-11",
        bed_type="general",
        ward_uuid=GENERAL,
        ward_name="General Ward",
    )
    assert ward.call_count == 1


@respx.mock
async def test_current_bed_missing_from_layout_is_loud() -> None:
    _login()
    respx.get(BEDS).mock(return_value=httpx.Response(200, json=_fixture("beds_by_patient.json")))
    full = _fixture("admission_location_full.json")
    full["bedLayouts"] = [b for b in full["bedLayouts"] if b["bedId"] != 11]
    respx.get(f"{WARDS}/{GENERAL}").mock(return_value=httpx.Response(200, json=full))
    with pytest.raises(VendorRejected):
        await _client().current_bed(PATIENT)
