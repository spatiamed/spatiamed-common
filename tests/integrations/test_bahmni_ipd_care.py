"""Offline tests for the Bahmni IPD care client (ward board, nursing chart, medication chart)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_http import BahmniHttp, to_epoch_ms
from sm_common.integrations.adapters.bahmni_ipd_care import BahmniIpdCareClient, NursingConfig
from sm_common.integrations.exceptions import (
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.ipd_care_types import (
    IntakeOutputWrite,
    NursingWriteOutcome,
    VitalsWrite,
    WardBed,
    WardLayout,
    WardPatient,
)

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


# ─── Nursing observations ───────────────────────────────────────────────────

ENCOUNTER = f"{REST}/bahmnicore/bahmniencounter"
VISIT = f"{REST}/visit"
CLIENT_REF = UUID("2c05f0f9-ba5c-47d4-95d3-f932678fc1a1")
OTHER_REF = UUID("759fcee5-d301-44c9-9066-45d3e9a26fa3")
PROVIDER = "aedd07c0-787b-47e2-ad63-3ea36266f1a1"
WARD_LOCATION = "69f420a7-68df-4979-b6c5-ce10721eefb4"
NURSING_ET = "d98c4111-9352-490a-a489-4719915fd5c9"
NOTE_CONCEPT = "6a81cc24-d4f6-41ee-8ab9-bab701f32a92"
IN_SET = "d2a8bf32-d22c-4a5c-a0fb-504a33c00db5"
OUT_SET = "dd39d0c5-a2f6-47fc-8368-2481718a8f62"
IO_ROUTE = "a98f12d0-f21c-4c3e-9630-711fb1ce8751"
IO_QTY = "4a5c1c53-0e4c-4126-aad4-79a189d5d1d2"
IO_TIME = "60ae43bc-06b3-45f7-8d54-489cba0fd079"
OBS_1 = "a8e767b8-b016-41ff-9431-422dc7e1fa88"
OBS_2 = "9a325bbf-25b5-4b9e-83f7-9592259bb860"
OBS_3 = "1dd1f6ae-ee02-4b51-8e1d-4f89a1a0ab6e"
AT = datetime(2026, 10, 20, 6, 30, tzinfo=timezone.utc)
AT_MS = 1792477800000
MARKER = f"sm:{CLIENT_REF}"
IO_SET_REP = "custom:(setMembers:(uuid,display,datatype:(display)))"


def _nursing_config(**overrides: Any) -> NursingConfig:
    base: dict[str, Any] = {
        "vitals_concepts": VITALS,
        "intake_concept_set_uuid": IN_SET,
        "output_concept_set_uuid": OUT_SET,
        "nursing_note_concept_uuid": NOTE_CONCEPT,
        "nursing_encounter_type_uuid": NURSING_ET,
    }
    base.update(overrides)
    return NursingConfig.from_mapping(base)


def _who(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "patient_uuid": PATIENT_A,
        "visit_uuid": VISIT_A,
        "provider_uuid": PROVIDER,
        "location_uuid": WARD_LOCATION,
        "recorded_at": AT,
        "client_ref": CLIENT_REF,
    }
    base.update(overrides)
    return base


def _sent(route: respx.Route) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


def _echo(obs_uuids: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "encounterUuid": str(CLIENT_REF),
            "observations": [{"uuid": u, "groupMembers": []} for u in obs_uuids],
        },
    )


def _io_set(members: list[tuple[str, str]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "setMembers": [
                {"uuid": uuid, "display": f"member {i}", "datatype": {"display": dt}}
                for i, (uuid, dt) in enumerate(members)
            ]
        },
    )


@respx.mock
async def test_vitals_concept_mapping_exact() -> None:
    _login()
    route = respx.post(ENCOUNTER).mock(
        return_value=httpx.Response(200, json=_fixture("nursing_encounter_response.json"))
    )
    out = await _client(_nursing_config()).write_vitals(
        **_who(), vitals=VitalsWrite(pulse=88, bp_systolic=120, bp_diastolic=80)
    )
    assert out == NursingWriteOutcome(
        encounter_uuid=str(CLIENT_REF), obs_uuids=(OBS_1, OBS_2, OBS_3), created=True
    )
    body = _sent(route)
    # Every field lands on its own configured concept; unset fields are absent.
    assert [(o["concept"]["uuid"], o["value"]) for o in body["observations"]] == [
        (VITALS["pulse"], 88),
        (VITALS["bp_systolic"], 120),
        (VITALS["bp_diastolic"], 80),
    ]
    assert body["encounterUuid"] == str(CLIENT_REF)
    assert body["patientUuid"] == PATIENT_A
    assert body["visitUuid"] == VISIT_A
    assert body["locationUuid"] == WARD_LOCATION
    assert body["encounterTypeUuid"] == NURSING_ET
    assert body["encounterDateTime"] == AT_MS
    assert body["providers"] == [{"uuid": PROVIDER}]
    assert route.call_count == 1

    full = VitalsWrite(
        pulse=72,
        bp_systolic=130,
        bp_diastolic=85,
        temp_c=Decimal("37.2"),
        spo2=97,
        resp_rate=16,
        weight_kg=Decimal("68.5"),
        height_cm=Decimal("172"),
    )
    route.mock(return_value=_echo([OBS_1] * 8))
    await _client(_nursing_config()).write_vitals(**_who(), vitals=full)
    got = {o["concept"]["uuid"]: o["value"] for o in _sent(route)["observations"]}
    assert got == {
        VITALS["pulse"]: 72,
        VITALS["bp_systolic"]: 130,
        VITALS["bp_diastolic"]: 85,
        VITALS["temp_c"]: 37.2,
        VITALS["spo2"]: 97,
        VITALS["resp_rate"]: 16,
        VITALS["weight_kg"]: 68.5,
        VITALS["height_cm"]: 172,
    }


@pytest.mark.parametrize(
    "vitals",
    [
        VitalsWrite(pulse=19),
        VitalsWrite(pulse=251),
        VitalsWrite(bp_systolic=39),
        VitalsWrite(bp_systolic=301),
        VitalsWrite(bp_diastolic=19),
        VitalsWrite(bp_diastolic=201),
        VitalsWrite(temp_c=Decimal("29.9")),
        VitalsWrite(temp_c=Decimal("45.1")),
        VitalsWrite(spo2=49),
        VitalsWrite(spo2=101),
        VitalsWrite(resp_rate=3),
        VitalsWrite(resp_rate=81),
        VitalsWrite(weight_kg=Decimal("0.2")),
        VitalsWrite(weight_kg=Decimal("400.1")),
        VitalsWrite(height_cm=Decimal("19.9")),
        VitalsWrite(height_cm=Decimal("250.5")),
        VitalsWrite(temp_c=Decimal("NaN")),
        VitalsWrite(pulse=True),
        VitalsWrite(),
    ],
)
async def test_vitals_out_of_range_is_value_error_before_io(vitals: VitalsWrite) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await _client(_nursing_config()).write_vitals(**_who(), vitals=vitals)
        assert not mock.calls


@respx.mock
async def test_vitals_range_bounds_inclusive() -> None:
    _login()
    route = respx.post(ENCOUNTER).mock(return_value=_echo([OBS_1] * 8))
    for vitals in (
        VitalsWrite(
            pulse=20,
            bp_systolic=40,
            bp_diastolic=20,
            temp_c=Decimal("30.0"),
            spo2=50,
            resp_rate=4,
            weight_kg=Decimal("0.3"),
            height_cm=Decimal("20"),
        ),
        VitalsWrite(
            pulse=250,
            bp_systolic=300,
            bp_diastolic=200,
            temp_c=Decimal("45.0"),
            spo2=100,
            resp_rate=80,
            weight_kg=Decimal("400"),
            height_cm=Decimal("250"),
        ),
    ):
        await _client(_nursing_config()).write_vitals(**_who(), vitals=vitals)
    assert route.call_count == 2


@pytest.mark.parametrize(("systolic", "diastolic"), [(120, 120), (100, 140)])
async def test_diastolic_not_below_systolic_refused(systolic: int, diastolic: int) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="diastolic"):
            await _client(_nursing_config()).write_vitals(
                **_who(), vitals=VitalsWrite(bp_systolic=systolic, bp_diastolic=diastolic)
            )
        assert not mock.calls


async def test_vitals_config_and_inputs_checked_before_io() -> None:
    missing_pulse = {k: v for k, v in VITALS.items() if k != "pulse"}
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ConfigError, match="pulse"):
            await _client(_nursing_config(vitals_concepts=missing_pulse)).write_vitals(
                **_who(), vitals=VitalsWrite(pulse=80)
            )
        with pytest.raises(ConfigError, match="nursing_encounter_type_uuid"):
            await _client(_nursing_config(nursing_encounter_type_uuid="")).write_vitals(
                **_who(), vitals=VitalsWrite(pulse=80)
            )
        with pytest.raises(ValueError):
            await _client(_nursing_config()).write_vitals(
                **_who(recorded_at=datetime(2026, 10, 20, 6, 30)), vitals=VitalsWrite(pulse=80)
            )
        with pytest.raises(ValueError):
            await _client(_nursing_config()).write_vitals(
                **_who(visit_uuid="../session"), vitals=VitalsWrite(pulse=80)
            )
        assert not mock.calls


@respx.mock
async def test_first_obs_carries_marker_comment() -> None:
    _login()
    route = respx.post(ENCOUNTER).mock(return_value=_echo([OBS_1, OBS_2]))
    await _client(_nursing_config()).write_vitals(
        **_who(), vitals=VitalsWrite(spo2=96, resp_rate=18)
    )
    obs = _sent(route)["observations"]
    assert obs[0]["comment"] == MARKER
    assert all("comment" not in o for o in obs[1:])

    route.mock(return_value=_echo([OBS_1]))
    await _client(_nursing_config()).write_nursing_note(**_who(), text="Slept well overnight.")
    obs = _sent(route)["observations"]
    assert obs == [
        {"concept": {"uuid": NOTE_CONCEPT}, "value": "Slept well overnight.", "comment": MARKER}
    ]


@respx.mock
async def test_intake_output_group_members_resolved_from_set() -> None:
    _login()
    in_set = respx.get(f"{CONCEPT}/{IN_SET}").mock(
        return_value=_io_set([(IO_TIME, "Datetime"), (IO_QTY, "Numeric"), (IO_ROUTE, "Text")])
    )
    route = respx.post(ENCOUNTER).mock(
        return_value=httpx.Response(
            200,
            json={
                "encounterUuid": str(CLIENT_REF),
                "observations": [
                    {
                        "uuid": OBS_1,
                        "groupMembers": [{"uuid": OBS_2}, {"uuid": OBS_3}, {"uuid": OTHER_REF.hex}],
                    }
                ],
            },
        )
    )
    taken = datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)
    client = _client(_nursing_config())
    out = await client.write_intake_output(
        **_who(),
        entry=IntakeOutputWrite(kind="intake", route="Oral", quantity_ml=250, recorded_at=taken),
    )
    assert out == NursingWriteOutcome(
        encounter_uuid=str(CLIENT_REF), obs_uuids=(OBS_1,), created=True
    )
    assert in_set.calls.last.request.url.params["v"] == IO_SET_REP
    (group,) = _sent(route)["observations"]
    assert group["concept"] == {"uuid": IN_SET}
    assert group["comment"] == MARKER
    members = {m["concept"]["uuid"]: m["value"] for m in group["groupMembers"]}
    assert members == {IO_ROUTE: "Oral", IO_QTY: 250, IO_TIME: to_epoch_ms(taken)}
    assert all("comment" not in m for m in group["groupMembers"])

    # The set is read once per client; output reads its own set.
    await client.write_intake_output(
        **_who(),
        entry=IntakeOutputWrite(kind="intake", route="IV", quantity_ml=500, recorded_at=taken),
    )
    assert in_set.call_count == 1
    out_set = respx.get(f"{CONCEPT}/{OUT_SET}").mock(
        return_value=_io_set([(IO_ROUTE, "Text"), (IO_QTY, "Numeric"), (IO_TIME, "Datetime")])
    )
    await client.write_intake_output(
        **_who(),
        entry=IntakeOutputWrite(kind="output", route="Urine", quantity_ml=0, recorded_at=taken),
    )
    assert out_set.call_count == 1
    assert _sent(route)["observations"][0]["concept"] == {"uuid": OUT_SET}


@pytest.mark.parametrize(
    "members",
    [
        [(IO_ROUTE, "Text"), (IO_QTY, "Numeric")],
        [(IO_ROUTE, "Text"), (IO_TIME, "Datetime")],
        [(IO_QTY, "Numeric"), (IO_TIME, "Datetime")],
        [(IO_ROUTE, "Text"), (IO_QTY, "Numeric"), (IO_TIME, "Datetime"), (OBS_1, "Numeric")],
        [],
    ],
)
@respx.mock
async def test_io_set_missing_members_is_config_error(members: list[tuple[str, str]]) -> None:
    _login()
    respx.get(f"{CONCEPT}/{IN_SET}").mock(return_value=_io_set(members))
    post = respx.post(ENCOUNTER).mock(return_value=_echo([OBS_1]))
    entry = IntakeOutputWrite(kind="intake", route="Oral", quantity_ml=100, recorded_at=AT)
    with pytest.raises(ConfigError, match="io_concepts_incomplete"):
        await _client(_nursing_config()).write_intake_output(**_who(), entry=entry)
    assert not post.called


@respx.mock
async def test_io_set_unknown_is_config_error() -> None:
    _login()
    respx.get(f"{CONCEPT}/{IN_SET}").mock(return_value=httpx.Response(404))
    post = respx.post(ENCOUNTER).mock(return_value=_echo([OBS_1]))
    entry = IntakeOutputWrite(kind="intake", route="Oral", quantity_ml=100, recorded_at=AT)
    with pytest.raises(ConfigError, match="io_concepts_incomplete"):
        await _client(_nursing_config()).write_intake_output(**_who(), entry=entry)
    assert not post.called


@pytest.mark.parametrize(
    ("entry", "config", "error"),
    [
        (IntakeOutputWrite("intake", "Oral", -1, AT), {}, ValueError),
        (IntakeOutputWrite("intake", "Oral", 10001, AT), {}, ValueError),
        (IntakeOutputWrite("intake", "  ", 100, AT), {}, ValueError),
        (IntakeOutputWrite("intake", "x" * 256, 100, AT), {}, ValueError),
        (IntakeOutputWrite("intake", "Oral", 100, datetime(2026, 10, 20)), {}, ValueError),
        (IntakeOutputWrite("drain", "Oral", 100, AT), {}, ValueError),  # type: ignore[arg-type]
        (
            IntakeOutputWrite("intake", "Oral", 100, AT),
            {"intake_concept_set_uuid": ""},
            ConfigError,
        ),
        (
            IntakeOutputWrite("output", "Urine", 100, AT),
            {"output_concept_set_uuid": ""},
            ConfigError,
        ),
        (
            IntakeOutputWrite("intake", "Oral", 100, AT),
            {"nursing_encounter_type_uuid": ""},
            ConfigError,
        ),
    ],
)
async def test_intake_output_checked_before_io(
    entry: IntakeOutputWrite, config: dict[str, Any], error: type[Exception]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(error):
            await _client(_nursing_config(**config)).write_intake_output(**_who(), entry=entry)
        assert not mock.calls


@respx.mock
async def test_nursing_note_length_limits() -> None:
    _login()
    route = respx.post(ENCOUNTER).mock(return_value=_echo([OBS_1]))
    client = _client(_nursing_config())
    for text in ("x", "y" * 4000):
        await client.write_nursing_note(**_who(), text=text)
        assert _sent(route)["observations"][0]["value"] == text
    assert route.call_count == 2

    with respx.mock(assert_all_called=False) as mock:
        for bad in ("", "   \n", "z" * 4001):
            with pytest.raises(ValueError):
                await client.write_nursing_note(**_who(), text=bad)
        with pytest.raises(ConfigError, match="nursing_note_concept_uuid"):
            await _client(_nursing_config(nursing_note_concept_uuid="")).write_nursing_note(
                **_who(), text="ok"
            )
        assert not mock.calls


@respx.mock
async def test_find_write_by_ref_hit_and_miss() -> None:
    _login()
    route = respx.get(f"{VISIT}/{VISIT_A}").mock(
        return_value=httpx.Response(200, json=_fixture("visit_obs_with_marker.json"))
    )
    client = _client(_nursing_config())
    hit = await client.find_write_by_ref(VISIT_A, CLIENT_REF)
    assert hit == NursingWriteOutcome(
        encounter_uuid=str(CLIENT_REF), obs_uuids=(OBS_1, OBS_2, OBS_3), created=False
    )
    assert route.calls.last.request.url.params["v"] == (
        "custom:(encounters:(uuid,obs:(uuid,comment)))"
    )
    other = await client.find_write_by_ref(VISIT_A, OTHER_REF)
    assert other is not None and other.encounter_uuid == "ac993197-bc51-4e18-8739-68ba69dead01"

    miss = UUID("85874928-01ba-4c8a-a397-8ff86d56ecc3")
    assert await client.find_write_by_ref(VISIT_A, miss) is None

    respx.get(f"{VISIT}/{VISIT_B}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await client.find_write_by_ref(VISIT_B, CLIENT_REF)

    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await client.find_write_by_ref("../session", CLIENT_REF)
        assert not mock.calls


@respx.mock
async def test_write_500_is_transient_not_retried() -> None:
    _login()
    route = respx.post(ENCOUNTER).mock(return_value=httpx.Response(500, text="<html>error</html>"))
    visit = respx.get(f"{VISIT}/{VISIT_A}").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(TransientError):
        await _client(_nursing_config()).write_vitals(**_who(), vitals=VitalsWrite(pulse=80))
    assert route.call_count == 1
    assert not visit.called


@respx.mock
async def test_write_response_naming_another_encounter_is_landed_rejection() -> None:
    _login()
    respx.post(ENCOUNTER).mock(
        return_value=httpx.Response(
            200, json={"encounterUuid": str(OTHER_REF), "observations": [{"uuid": OBS_1}]}
        )
    )
    with pytest.raises(VendorRejected) as info:
        await _client(_nursing_config()).write_vitals(**_who(), vitals=VitalsWrite(pulse=80))
    assert info.value.landed is True


@respx.mock
async def test_write_route_404_is_not_found() -> None:
    _login()
    respx.post(ENCOUNTER).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client(_nursing_config()).write_vitals(**_who(), vitals=VitalsWrite(pulse=80))
