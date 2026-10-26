"""Offline tests for the Bahmni IPD care client (ward board, nursing chart, medication chart)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_ipd_care import BahmniIpdCareClient, NursingConfig
from sm_common.integrations.exceptions import ConfigError, NotFound, TransientError
from sm_common.integrations.ipd_care_types import WardBed, WardLayout, WardPatient

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
WARDS = f"{REST}/admissionLocation"
LOCATION = f"{REST}/location"
CONCEPT = f"{REST}/concept"
IPD_WARDS = f"{REST}/ipd/wards"
FIX = Path(__file__).parent / "fixtures" / "bahmni" / "ipd_care"

WARD = "6c3c8e9b-542e-489f-94d5-68de43d2d4f2"
PATIENT_A = "7b6e3725-544e-4850-a807-08db11c429ce"
PATIENT_B = "4809f4e0-6c38-48f9-97d9-a9d55ad63b84"
VISIT_A = "a3f0c2d4-6b1e-4f7a-9c8d-2e5b7a1c3f90"
VISIT_B = "c81d5e3a-2f4b-4d6e-8a7c-9b0e1f2a3d45"

VITALS = {
    "pulse": "0f1e2d3c-4b5a-4968-8776-655443322110",
    "bp_systolic": "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d",
    "bp_diastolic": "2b3c4d5e-6f7a-4b2c-9d3e-4f5a6b7c8d9e",
    "temp_c": "3c4d5e6f-7a8b-4c3d-ae4f-5a6b7c8d9e0f",
    "spo2": "4d5e6f7a-8b9c-4d4e-bf5a-6b7c8d9e0f1a",
    "resp_rate": "5e6f7a8b-9c0d-4e5f-8a6b-7c8d9e0f1a2b",
    "weight_kg": "6f7a8b9c-0d1e-4f6a-9b7c-8d9e0f1a2b3c",
    "height_cm": "7a8b9c0d-1e2f-4a7b-8c8d-9e0f1a2b3c4d",
}


def _fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def _client(config: NursingConfig | None = None) -> BahmniIpdCareClient:
    return BahmniIpdCareClient(BahmniHttp(BASE, "u", "p"), config or NursingConfig())


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


# ─── Config ─────────────────────────────────────────────────────────────────


def test_config_from_mapping_exact_vitals_keys() -> None:
    empty = NursingConfig.from_mapping(None)
    assert empty == NursingConfig()
    assert empty.vitals_concepts == {}
    assert empty.intake_concept_set_uuid == ""
    assert empty.output_concept_set_uuid == ""
    assert empty.nursing_note_concept_uuid == ""
    assert empty.nursing_encounter_type_uuid == ""

    cfg = NursingConfig.from_mapping(
        {
            "vitals_concepts": VITALS,
            "intake_concept_set_uuid": "IN",
            "output_concept_set_uuid": "OUT",
            "nursing_note_concept_uuid": "NOTE",
            "nursing_encounter_type_uuid": "ET",
            # Keys the caller keeps for itself in the same mapping are ignored.
            "shift_starts": ["08:00", "20:00"],
            "vitals_due_every_hours": 4,
        }
    )
    assert dict(cfg.vitals_concepts) == VITALS
    assert set(cfg.vitals_concepts) == {
        "pulse",
        "bp_systolic",
        "bp_diastolic",
        "temp_c",
        "spo2",
        "resp_rate",
        "weight_kg",
        "height_cm",
    }
    assert cfg.intake_concept_set_uuid == "IN"
    assert cfg.output_concept_set_uuid == "OUT"
    assert cfg.nursing_note_concept_uuid == "NOTE"
    assert cfg.nursing_encounter_type_uuid == "ET"
    # A subset of vitals is accepted; missing ones are checked when written.
    partial = NursingConfig.from_mapping({"vitals_concepts": {"pulse": VITALS["pulse"]}})
    assert dict(partial.vitals_concepts) == {"pulse": VITALS["pulse"]}


def test_unknown_vitals_key_is_config_error() -> None:
    with pytest.raises(ConfigError, match="bp_sys"):
        NursingConfig.from_mapping({"vitals_concepts": {"bp_sys": "X"}})
    with pytest.raises(ConfigError, match="vitals_concepts"):
        NursingConfig.from_mapping({"vitals_concepts": ["pulse"]})
    with pytest.raises(ConfigError, match="pulse"):
        NursingConfig.from_mapping({"vitals_concepts": {"pulse": 7}})
    with pytest.raises(ConfigError, match="nursing_encounter_type_uuid"):
        NursingConfig.from_mapping({"nursing_encounter_type_uuid": 5})


# ─── Ward layout ────────────────────────────────────────────────────────────


@respx.mock
async def test_layout_keys_beds_on_bed_id_and_tolerates_null_bed_id_cells() -> None:
    _login()
    route = respx.get(f"{WARDS}/{WARD}").mock(
        return_value=httpx.Response(200, json=_fixture("ward_full.json"))
    )
    layout = await _client().ward_layout(WARD)

    assert route.calls.last.request.url.params["v"] == "full"
    assert isinstance(layout, WardLayout)
    assert layout.ward_uuid == WARD
    assert layout.ward_name == "Test Ward"
    assert (layout.rows, layout.columns) == (3, 4)
    # 12 cells, one of them a gap (bedId null): 11 beds, every one keyed on its id.
    assert len(layout.beds) == 11
    ids = [b.bed_id for b in layout.beds]
    assert ids == list(range(101, 112))
    assert all(isinstance(b, WardBed) and isinstance(b.bed_id, int) for b in layout.beds)
    assert (2, 3) not in {(b.row, b.column) for b in layout.beds}

    by_id = {b.bed_id: b for b in layout.beds}
    # Two beds reporting the same uuid in one read stay two beds, each with its own occupant.
    assert by_id[105].bed_uuid == by_id[106].bed_uuid
    assert by_id[105].patient_uuids == ()
    assert by_id[106].patient_uuids == (PATIENT_B,)
    assert by_id[106].status == "OCCUPIED"
    # A freed bed may come back with no uuid and no bed type.
    assert by_id[104].bed_uuid is None
    assert by_id[104].bed_type is None
    assert by_id[104].bed_number == "TW-104"
    assert by_id[101].bed_type == "general"


@respx.mock
async def test_layout_occupants_and_tags_parsed() -> None:
    _login()
    respx.get(f"{WARDS}/{WARD}").mock(
        return_value=httpx.Response(200, json=_fixture("ward_full.json"))
    )
    layout = await _client().ward_layout(WARD)
    assert layout is not None
    by_id = {b.bed_id: b for b in layout.beds}

    occupied = {b.bed_id: b.patient_uuids for b in layout.beds if b.status == "OCCUPIED"}
    assert occupied == {101: (PATIENT_A,), 106: (PATIENT_B,)}
    assert by_id[101].row == 1 and by_id[101].column == 1
    assert by_id[111].tags == ("Isolation",)
    assert all(b.tags == () for b in layout.beds if b.bed_id != 111)
    assert {b.status for b in layout.beds} == {"AVAILABLE", "OCCUPIED"}


@respx.mock
async def test_unknown_ward_404_returns_none() -> None:
    # Bahmni answers an unknown ward uuid with HTTP 500; the core location route says 404.
    _login()
    respx.get(f"{WARDS}/{WARD}").mock(return_value=httpx.Response(500, text="<html>error</html>"))
    loc = respx.get(f"{LOCATION}/{WARD}").mock(return_value=httpx.Response(404))
    assert await _client().ward_layout(WARD) is None
    assert loc.called


@respx.mock
async def test_ward_layout_500_on_known_location_stays_transient() -> None:
    _login()
    respx.get(f"{WARDS}/{WARD}").mock(return_value=httpx.Response(500, text="boom"))
    respx.get(f"{LOCATION}/{WARD}").mock(return_value=httpx.Response(200, json={"uuid": WARD}))
    with pytest.raises(TransientError):
        await _client().ward_layout(WARD)


@respx.mock
async def test_ward_layout_route_404_is_not_found() -> None:
    # A 404 on admissionLocation itself means the bed-management route is missing.
    _login()
    respx.get(f"{WARDS}/{WARD}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().ward_layout(WARD)


async def test_ward_layout_bad_uuid_refused_before_io() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await _client().ward_layout("../session")
        assert not mock.calls


# ─── Concept datatypes ──────────────────────────────────────────────────────


@respx.mock
async def test_concept_datatypes_maps_each_uuid() -> None:
    _login()
    numeric, text, unknown = VITALS["pulse"], VITALS["spo2"], VITALS["height_cm"]
    r1 = respx.get(f"{CONCEPT}/{numeric}").mock(
        return_value=httpx.Response(200, json={"uuid": numeric, "datatype": {"display": "Numeric"}})
    )
    respx.get(f"{CONCEPT}/{text}").mock(
        return_value=httpx.Response(200, json={"uuid": text, "datatype": {"display": "Text"}})
    )
    got = await _client().concept_datatypes([numeric, text, numeric])
    assert got == {numeric: "Numeric", text: "Text"}
    # Each uuid read once, with a rep that names only the datatype.
    assert r1.call_count == 1
    assert r1.calls.last.request.url.params["v"] == "custom:(uuid,datatype:(display))"

    respx.get(f"{CONCEPT}/{unknown}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound, match=unknown):
        await _client().concept_datatypes([numeric, unknown])

    with respx.mock(assert_all_called=False) as mock:
        assert await _client().concept_datatypes([]) == {}
        with pytest.raises(ValueError):
            await _client().concept_datatypes(["not a uuid/../x"])
        assert not mock.calls


# ─── Ward patients ──────────────────────────────────────────────────────────


@respx.mock
async def test_ward_patients_paging_params_and_parse() -> None:
    _login()
    route = respx.get(f"{IPD_WARDS}/{WARD}/patients").mock(
        return_value=httpx.Response(200, json=_fixture("ward_patients.json"))
    )
    rows = await _client().ward_patients(WARD, offset=10, limit=25)

    params = route.calls.last.request.url.params
    assert (params["offset"], params["limit"]) == ("10", "25")
    assert rows == [
        WardPatient(
            patient_uuid=PATIENT_A,
            visit_uuid=VISIT_A,
            bed_id=101,
            bed_number="TW-101",
            admitted_at=datetime.fromtimestamp(1792915200, tz=timezone.utc),
            display_name="Test Patient",
        ),
        WardPatient(
            patient_uuid=PATIENT_B,
            visit_uuid=VISIT_B,
            bed_id=None,
            bed_number=None,
            admitted_at=None,
            display_name="Sample Patient",
        ),
    ]
    assert rows[0].admitted_at is not None and rows[0].admitted_at.tzinfo is not None

    default = await _client().ward_patients(WARD)
    assert len(default) == 2
    params = route.calls.last.request.url.params
    assert (params["offset"], params["limit"]) == ("0", "50")


@pytest.mark.parametrize(("offset", "limit"), [(0, 0), (0, 201), (-1, 50), (0, -5)])
async def test_ward_patients_paging_validated_before_io(offset: int, limit: int) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await _client().ward_patients(WARD, offset=offset, limit=limit)
        assert not mock.calls


@respx.mock
async def test_ward_patients_unknown_ward_route_404_is_not_found() -> None:
    _login()
    respx.get(f"{IPD_WARDS}/{WARD}/patients").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().ward_patients(WARD)
