"""Offline tests for the Bahmni IPD care client (ward board, nursing chart, medication chart)."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone
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
    AuthError,
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.ipd_care_types import (
    IntakeOutputWrite,
    MarAdministrationOutcome,
    MarAdministrationWrite,
    MarScheduleOutcome,
    MarScheduleWrite,
    MarSlot,
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
ENCOUNTER_READ = f"{REST}/encounter"
ENCOUNTER_REP = "custom:(uuid,visit:(uuid),obs:(uuid,comment))"
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


def _no_earlier_write() -> respx.Route:
    """The pre-read of the write's own encounter answers 404: nothing written yet."""
    return respx.get(f"{ENCOUNTER_READ}/{CLIENT_REF}").mock(return_value=httpx.Response(404))


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
    _no_earlier_write()
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
    _no_earlier_write()
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
        for bad in ("patient_uuid", "visit_uuid", "provider_uuid", "location_uuid"):
            with pytest.raises(ValueError, match=bad):
                await _client(_nursing_config()).write_vitals(
                    **_who(**{bad: "../session"}), vitals=VitalsWrite(pulse=80)
                )
        assert not mock.calls


@respx.mock
async def test_first_obs_carries_marker_comment() -> None:
    _login()
    _no_earlier_write()
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
    _no_earlier_write()
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


@pytest.mark.parametrize(
    "who",
    [
        {"patient_uuid": "../session"},
        {"visit_uuid": "../session"},
        {"provider_uuid": "../session"},
        {"location_uuid": "../session"},
        {"location_uuid": ""},
        {"recorded_at": datetime(2026, 10, 20, 6, 30)},
    ],
)
async def test_intake_output_caller_fields_checked_before_io(who: dict[str, Any]) -> None:
    # Checked before the set read as well as before the write.
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await _client(_nursing_config()).write_intake_output(**_who(**who), entry=_io_entry())
        assert not mock.calls


@respx.mock
async def test_nursing_note_length_limits() -> None:
    _login()
    _no_earlier_write()
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
    route = respx.get(f"{ENCOUNTER_READ}/{CLIENT_REF}").mock(
        return_value=httpx.Response(200, json=_fixture("encounter_with_marker.json"))
    )
    client = _client(_nursing_config())
    hit = await client.find_write_by_ref(VISIT_A, CLIENT_REF)
    assert hit == NursingWriteOutcome(
        encounter_uuid=str(CLIENT_REF), obs_uuids=(OBS_1, OBS_2, OBS_3), created=False
    )
    # One read of the write's own encounter, never a scan of the visit.
    assert route.calls.last.request.url.params["v"] == ENCOUNTER_REP

    respx.get(f"{ENCOUNTER_READ}/{OTHER_REF}").mock(return_value=httpx.Response(404))
    assert await client.find_write_by_ref(VISIT_A, OTHER_REF) is None

    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await client.find_write_by_ref("../session", CLIENT_REF)
        with pytest.raises(ValueError):
            await client.find_write_by_ref(VISIT_A, str(CLIENT_REF))  # type: ignore[arg-type]
        assert not mock.calls


@respx.mock
async def test_find_write_by_ref_hit_without_marker_still_counts() -> None:
    # The encounter uuid is the caller's ref, so the encounter existing is the proof;
    # a marker obs voided (or edited) in the hospital record must not invite a re-send.
    _login()
    body = _fixture("encounter_with_marker.json")
    body["obs"] = [{"uuid": OBS_2, "comment": f"note {MARKER}-x"}]
    respx.get(f"{ENCOUNTER_READ}/{CLIENT_REF}").mock(return_value=httpx.Response(200, json=body))
    hit = await _client(_nursing_config()).find_write_by_ref(VISIT_A, CLIENT_REF)
    assert hit == NursingWriteOutcome(
        encounter_uuid=str(CLIENT_REF), obs_uuids=(OBS_2,), created=False
    )


@respx.mock
async def test_find_write_by_ref_in_another_visit_is_rejected() -> None:
    _login()
    respx.get(f"{ENCOUNTER_READ}/{CLIENT_REF}").mock(
        return_value=httpx.Response(200, json=_fixture("encounter_with_marker.json"))
    )
    with pytest.raises(VendorRejected) as info:
        await _client(_nursing_config()).find_write_by_ref(VISIT_B, CLIENT_REF)
    assert info.value.landed is False


@respx.mock
async def test_find_write_by_ref_unreadable_encounter_is_rejected() -> None:
    _login()
    respx.get(f"{ENCOUNTER_READ}/{CLIENT_REF}").mock(
        return_value=httpx.Response(200, json={"uuid": str(OTHER_REF), "visit": {"uuid": VISIT_A}})
    )
    with pytest.raises(VendorRejected):
        await _client(_nursing_config()).find_write_by_ref(VISIT_A, CLIENT_REF)


def _io_entry() -> IntakeOutputWrite:
    return IntakeOutputWrite(kind="intake", route="Oral", quantity_ml=250, recorded_at=AT)


_WRITES: list[tuple[str, str, Any]] = [
    ("vitals", "write_vitals", lambda: {"vitals": VitalsWrite(pulse=80)}),
    ("io", "write_intake_output", lambda: {"entry": _io_entry()}),
    ("note", "write_nursing_note", lambda: {"text": "Comfortable, no complaints."}),
]


@pytest.mark.parametrize(("kind", "method", "args"), _WRITES)
@respx.mock
async def test_retry_after_unknown_outcome_finds_earlier_write_and_does_not_post(
    kind: str, method: str, args: Any
) -> None:
    # The POST commits but its answer is lost (a gateway 502): the caller retries with
    # the same client_ref, and the retry must find the write instead of charting twice.
    _login()
    respx.get(f"{CONCEPT}/{IN_SET}").mock(
        return_value=_io_set([(IO_ROUTE, "Text"), (IO_QTY, "Numeric"), (IO_TIME, "Datetime")])
    )
    earlier = respx.get(f"{ENCOUNTER_READ}/{CLIENT_REF}").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=_fixture("encounter_with_marker.json")),
        ]
    )
    post = respx.post(ENCOUNTER).mock(return_value=httpx.Response(502, text="bad gateway"))
    client = _client(_nursing_config())
    with pytest.raises(TransientError):
        await getattr(client, method)(**_who(), **args())
    out = await getattr(client, method)(**_who(), **args())
    assert out == NursingWriteOutcome(
        encounter_uuid=str(CLIENT_REF), obs_uuids=(OBS_1, OBS_2, OBS_3), created=False
    )
    assert post.call_count == 1
    assert earlier.call_count == 2
    assert earlier.calls.last.request.url.params["v"] == ENCOUNTER_REP


@pytest.mark.parametrize(("kind", "method", "args"), _WRITES)
@respx.mock
async def test_write_whose_ref_names_an_encounter_in_another_visit_is_refused(
    kind: str, method: str, args: Any
) -> None:
    _login()
    respx.get(f"{CONCEPT}/{IN_SET}").mock(
        return_value=_io_set([(IO_ROUTE, "Text"), (IO_QTY, "Numeric"), (IO_TIME, "Datetime")])
    )
    respx.get(f"{ENCOUNTER_READ}/{CLIENT_REF}").mock(
        return_value=httpx.Response(200, json=_fixture("encounter_with_marker.json"))
    )
    post = respx.post(ENCOUNTER).mock(return_value=_echo([OBS_1]))
    with pytest.raises(VendorRejected) as info:
        await getattr(_client(_nursing_config()), method)(**_who(visit_uuid=VISIT_B), **args())
    assert info.value.landed is False
    assert not post.called


@pytest.mark.parametrize(("kind", "method", "args"), _WRITES)
@respx.mock
async def test_pre_read_5xx_is_transient_and_nothing_posted(
    kind: str, method: str, args: Any
) -> None:
    _login()
    respx.get(f"{CONCEPT}/{IN_SET}").mock(
        return_value=_io_set([(IO_ROUTE, "Text"), (IO_QTY, "Numeric"), (IO_TIME, "Datetime")])
    )
    respx.get(f"{ENCOUNTER_READ}/{CLIENT_REF}").mock(return_value=httpx.Response(503))
    post = respx.post(ENCOUNTER).mock(return_value=_echo([OBS_1]))
    with pytest.raises(TransientError):
        await getattr(_client(_nursing_config()), method)(**_who(), **args())
    assert not post.called


@respx.mock
async def test_write_500_is_transient_not_retried() -> None:
    _login()
    earlier = _no_earlier_write()
    route = respx.post(ENCOUNTER).mock(return_value=httpx.Response(500, text="<html>error</html>"))
    visit = respx.get(f"{VISIT}/{VISIT_A}").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(TransientError):
        await _client(_nursing_config()).write_vitals(**_who(), vitals=VitalsWrite(pulse=80))
    assert route.call_count == 1
    assert earlier.call_count == 1
    assert not visit.called


@respx.mock
async def test_write_response_naming_another_encounter_is_landed_rejection() -> None:
    _login()
    _no_earlier_write()
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
    _no_earlier_write()
    respx.post(ENCOUNTER).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client(_nursing_config()).write_vitals(**_who(), vitals=VitalsWrite(pulse=80))


# ─── Medication chart: schedule read and create ─────────────────────────────

MED = f"{REST}/ipd/schedule/type/medication"
ORDER_A = "5d1c7a2e-3b4f-4e6a-9c8d-7f1e2a3b4c5d"
PROVIDER = "e4f5a6b7-c8d9-4e0f-a1b2-c3d4e5f6a7b8"
IST = timezone(timedelta(hours=5, minutes=30))


def _epoch_s(dt: datetime) -> int:
    return to_epoch_ms(dt) // 1000


def _utc(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _schedule_write(**over: Any) -> MarScheduleWrite:
    base: dict[str, Any] = {
        "patient_uuid": PATIENT_A,
        "order_uuid": ORDER_A,
        "provider_uuid": PROVIDER,
        "first_day_slots": (_utc(2026, 3, 2, 8, 30), _utc(2026, 3, 2, 14, 30)),
        "day_wise_slots": (
            _utc(2026, 3, 3, 2, 30),
            _utc(2026, 3, 3, 8, 30),
            _utc(2026, 3, 3, 14, 30),
        ),
        "remaining_day_slots": (_utc(2026, 3, 5, 2, 30),),
    }
    base.update(over)
    return MarScheduleWrite(**base)


@respx.mock
async def test_schedule_read_params_epoch_seconds_and_parse_statuses() -> None:
    _login()
    route = respx.get(MED).mock(
        return_value=httpx.Response(200, json=_fixture("medication_schedule.json"))
    )
    start = datetime(2026, 3, 2, 0, 0, tzinfo=IST)
    end = start + timedelta(days=1)
    slots = await _client().medication_schedule(PATIENT_A, start, end, visit_uuid=VISIT_A)

    params = dict(route.calls.last.request.url.params)
    # The module reads these as epoch SECONDS (UTC instants), not milliseconds.
    assert params == {
        "patientUuid": PATIENT_A,
        "startTime": str(_epoch_s(start)),
        "endTime": str(_epoch_s(end)),
        "visitUuid": VISIT_A,
    }
    assert params["startTime"] == "1772389800"  # 2026-03-01T18:30Z
    assert [s.status for s in slots] == ["MISSED", "COMPLETED", "NOT_DONE", "STOPPED", "SCHEDULED"]
    given = slots[1]
    assert given == MarSlot(
        slot_uuid="a1000000-0000-4000-8000-000000000001",
        order_uuid=ORDER_A,
        drug_name="Test Paracetamol 500 mg",
        service_type="MedicationRequest",
        start_at=_utc(2026, 3, 2, 2, 30),
        status="COMPLETED",
        administration_uuid="b1000000-0000-4000-8000-000000000001",
        # administeredDateTime is a java Date (epoch ms), unlike the slot time.
        administered_at=_utc(2026, 3, 2, 2, 41),
        notes=None,
    )
    assert slots[4].notes == "after food"
    assert slots[4].administration_uuid is None and slots[4].administered_at is None
    assert all(s.start_at.tzinfo is timezone.utc for s in slots)


@respx.mock
async def test_schedule_read_without_visit_omits_param_and_empty_list() -> None:
    _login()
    route = respx.get(MED).mock(return_value=httpx.Response(200, json=[]))
    start = _utc(2026, 3, 2, 0, 0)
    assert await _client().medication_schedule(PATIENT_A, start, start + timedelta(hours=6)) == []
    assert "visitUuid" not in dict(route.calls.last.request.url.params)


@respx.mock
async def test_schedule_read_flat_slot_list_is_rejected_not_empty() -> None:
    """A window answer in the per-order (flat) shape must not read as "nothing due"."""
    _login()
    respx.get(MED).mock(
        return_value=httpx.Response(200, json=_fixture("medication_schedule_by_order.json"))
    )
    start = _utc(2026, 3, 2, 0, 0)
    with pytest.raises(VendorRejected):
        await _client().medication_schedule(PATIENT_A, start, start + timedelta(days=1))


@respx.mock
async def test_schedule_read_unknown_status_is_rejected() -> None:
    _login()
    body = _fixture("medication_schedule.json")
    body[0]["slots"][0]["status"] = "PAUSED"
    respx.get(MED).mock(return_value=httpx.Response(200, json=body))
    start = _utc(2026, 3, 2, 0, 0)
    with pytest.raises(VendorRejected):
        await _client().medication_schedule(PATIENT_A, start, start + timedelta(days=1))


@respx.mock
async def test_slot_times_round_trip_utc_epoch_seconds() -> None:
    """A dose at 08:00 IST is sent and read back as the same instant (no 5h30m slip)."""
    _login()
    due = datetime(2026, 3, 2, 8, 0, tzinfo=IST)
    slot = {
        "uuid": "d1000000-0000-4000-8000-000000000001",
        "serviceType": "MedicationRequest",
        "status": "SCHEDULED",
        "startTime": 1772418600,  # what the module answers for the instant it stored
        "order": {"uuid": ORDER_A},
        "medicationAdministration": None,
        "notes": None,
    }
    respx.get(MED).mock(
        side_effect=[
            httpx.Response(200, json=[]),  # pre-read: not on the chart
            httpx.Response(200, json=[slot]),  # read-back after the create
            httpx.Response(200, json=[{"uuid": "s", "slots": [slot]}]),  # window read
        ]
    )
    post = respx.post(MED).mock(
        return_value=httpx.Response(200, json=_fixture("schedule_create_response.json"))
    )
    client = _client()
    await client.create_medication_schedule(
        _schedule_write(first_day_slots=(due,), day_wise_slots=(), remaining_day_slots=())
    )
    sent = json.loads(post.calls.last.request.content)
    assert sent["firstDaySlotsStartTime"] == [1772418600]  # 2026-03-02T02:30Z, seconds
    assert sent["slotStartTime"] == 1772418600
    [back] = await client.medication_schedule(PATIENT_A, due, due + timedelta(hours=1))
    assert back.start_at == due == _utc(2026, 3, 2, 2, 30)


@respx.mock
async def test_slot_time_read_back_equals_instant_sent() -> None:
    _login()
    due = datetime(2026, 3, 2, 8, 0, tzinfo=IST)
    respx.get(MED).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "uuid": "s1",
                    "slots": [
                        {
                            "uuid": "e1",
                            "serviceType": "MedicationRequest",
                            "status": "SCHEDULED",
                            "startTime": _epoch_s(due),
                            "order": {"uuid": ORDER_A},
                            "medicationAdministration": None,
                            "notes": None,
                        }
                    ],
                }
            ],
        )
    )
    [slot] = await _client().medication_schedule(PATIENT_A, due, due + timedelta(hours=1))
    assert slot.start_at == due
    assert slot.start_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("where", ["start", "end", "first_day", "day_wise", "remaining"])
@respx.mock
async def test_naive_datetime_refused(where: str) -> None:
    naive = datetime(2026, 3, 2, 8, 0)
    aware = _utc(2026, 3, 2, 8, 0)
    client = _client()
    with pytest.raises(ValueError):
        if where == "start":
            await client.medication_schedule(PATIENT_A, naive, aware)
        elif where == "end":
            await client.medication_schedule(PATIENT_A, aware, naive)
        elif where == "first_day":
            await client.create_medication_schedule(_schedule_write(first_day_slots=(naive,)))
        elif where == "day_wise":
            await client.create_medication_schedule(_schedule_write(day_wise_slots=(naive,)))
        else:
            await client.create_medication_schedule(_schedule_write(remaining_day_slots=(naive,)))
    assert len(respx.calls) == 0


@respx.mock
async def test_schedule_read_end_not_after_start_refused() -> None:
    start = _utc(2026, 3, 2, 8, 0)
    with pytest.raises(ValueError):
        await _client().medication_schedule(PATIENT_A, start, start)
    assert len(respx.calls) == 0


@respx.mock
async def test_create_schedule_body_shape() -> None:
    _login()
    pre = respx.get(MED).mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=_fixture("medication_schedule_by_order.json")),
        ]
    )
    post = respx.post(MED).mock(
        return_value=httpx.Response(200, json=_fixture("schedule_create_response.json"))
    )
    w = _schedule_write(comments="test chart")
    out = await _client().create_medication_schedule(w)

    assert dict(pre.calls[0].request.url.params) == {
        "patientUuid": PATIENT_A,
        "orderUuids": ORDER_A,
        "serviceType": "MEDICATION_REQUEST",
    }
    sent = json.loads(post.calls.last.request.content)
    assert sent == {
        "patientUuid": PATIENT_A,
        "orderUuid": ORDER_A,
        "providerUuid": PROVIDER,
        "comments": "test chart",
        "slotStartTime": _epoch_s(_utc(2026, 3, 2, 8, 30)),
        "firstDaySlotsStartTime": [_epoch_s(t) for t in w.first_day_slots],
        "dayWiseSlotsStartTime": [_epoch_s(t) for t in w.day_wise_slots],
        "remainingDaySlotsStartTime": [_epoch_s(t) for t in w.remaining_day_slots],
        "medicationFrequency": "FIXED_SCHEDULE_FREQUENCY",
        "serviceType": "MEDICATION_REQUEST",
    }
    # The create answer names no slots; they come from one read-back by order.
    assert out == MarScheduleOutcome(
        order_uuid=ORDER_A,
        created=True,
        slot_uuids=(
            "c1000000-0000-4000-8000-000000000001",
            "c1000000-0000-4000-8000-000000000002",
        ),
    )
    assert pre.call_count == 2 and post.call_count == 1


@respx.mock
async def test_create_as_needed_schedule_uses_placeholder_and_no_slots() -> None:
    _login()
    pre = respx.get(MED).mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(
                200,
                json=[
                    {
                        "uuid": "f1000000-0000-4000-8000-000000000001",
                        "serviceType": "AsNeededPlaceholder",
                        "status": "SCHEDULED",
                        # A placeholder starts at its order's effective start date.
                        "startTime": 1772418600,
                        "order": {"uuid": ORDER_A},
                        "medicationAdministration": None,
                        "notes": None,
                    }
                ],
            ),
        ]
    )
    post = respx.post(MED).mock(
        return_value=httpx.Response(200, json=_fixture("schedule_create_response.json"))
    )
    out = await _client().create_medication_schedule(
        _schedule_write(
            service_type="AsNeededMedicationRequest",
            first_day_slots=(),
            day_wise_slots=(),
            remaining_day_slots=(),
        )
    )
    # Only the placeholder service type makes the module chart an as-needed order,
    # and the module does not refuse a second placeholder: the pre-read must ask for it.
    assert dict(pre.calls[0].request.url.params)["serviceType"] == "AS_NEEDED_PLACEHOLDER"
    sent = json.loads(post.calls.last.request.content)
    assert sent["serviceType"] == "AS_NEEDED_PLACEHOLDER"
    assert sent["slotStartTime"] is None
    assert sent["firstDaySlotsStartTime"] == []
    assert out.created is True
    assert out.slot_uuids == ("f1000000-0000-4000-8000-000000000001",)


@pytest.mark.parametrize("service_type", ["MedicationRequest", "AsNeededMedicationRequest"])
@respx.mock
async def test_create_schedule_existing_order_returns_not_created_without_post(
    service_type: str,
) -> None:
    _login()
    body = _fixture("medication_schedule_by_order.json")
    over: dict[str, Any] = {"service_type": service_type}
    if service_type != "MedicationRequest":
        over.update(first_day_slots=(), day_wise_slots=(), remaining_day_slots=())
        for slot in body:
            slot["serviceType"] = "AsNeededPlaceholder"
    respx.get(MED).mock(return_value=httpx.Response(200, json=body))
    post = respx.post(MED).mock(return_value=httpx.Response(200, json={}))
    out = await _client().create_medication_schedule(_schedule_write(**over))
    assert out == MarScheduleOutcome(
        order_uuid=ORDER_A,
        created=False,
        slot_uuids=(
            "c1000000-0000-4000-8000-000000000001",
            "c1000000-0000-4000-8000-000000000002",
        ),
    )
    assert post.call_count == 0


@respx.mock
async def test_pre_read_slots_of_another_order_are_rejected() -> None:
    _login()
    body = _fixture("medication_schedule_by_order.json")
    body[0]["order"] = {"uuid": "8e2f1a3b-4c5d-4f6e-a7b8-9c0d1e2f3a4b"}
    respx.get(MED).mock(return_value=httpx.Response(200, json=body))
    post = respx.post(MED).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(VendorRejected):
        await _client().create_medication_schedule(_schedule_write())
    assert post.call_count == 0


@pytest.mark.parametrize(
    "over",
    [
        {"first_day_slots": (), "day_wise_slots": (), "remaining_day_slots": ()},
        {"service_type": "AsNeededMedicationRequest"},
        {"service_type": "EmergencyMedicationRequest"},
        {"order_uuid": "../visit"},
        {"provider_uuid": ""},
        {"comments": "x" * 1001},
    ],
)
@respx.mock
async def test_create_schedule_input_checked_before_io(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await _client().create_medication_schedule(_schedule_write(**over))
    assert len(respx.calls) == 0


@respx.mock
async def test_create_schedule_post_400_is_vendor_rejected() -> None:
    _login()
    respx.get(MED).mock(return_value=httpx.Response(200, json=[]))
    respx.post(MED).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "Slots already created for this drug order"}}
        )
    )
    with pytest.raises(VendorRejected):
        await _client().create_medication_schedule(_schedule_write())


def test_schedule_write_names_no_visit() -> None:
    """The module charts against the patient's active visit; the write cannot pick one."""
    assert "visit_uuid" not in {f.name for f in fields(MarScheduleWrite)}


@pytest.mark.parametrize(
    ("service_type", "answered"),
    [
        ("AsNeededMedicationRequest", "MedicationRequest"),
        ("MedicationRequest", "AsNeededPlaceholder"),
    ],
)
@respx.mock
async def test_pre_read_answering_another_service_type_is_rejected(
    service_type: str, answered: str
) -> None:
    """The by-order read filters on serviceType (absent means scheduled only), so a slot
    of another type means the filter was not applied and an empty answer proves nothing."""
    _login()
    body = _fixture("medication_schedule_by_order.json")
    for slot in body:
        slot["serviceType"] = answered
    respx.get(MED).mock(return_value=httpx.Response(200, json=body))
    post = respx.post(MED).mock(return_value=httpx.Response(200, json={}))
    over: dict[str, Any] = {"service_type": service_type}
    if service_type != "MedicationRequest":
        over.update(first_day_slots=(), day_wise_slots=(), remaining_day_slots=())
    with pytest.raises(VendorRejected):
        await _client().create_medication_schedule(_schedule_write(**over))
    assert post.call_count == 0


@pytest.mark.parametrize("service_type", ["MedicationRequest", "AsNeededMedicationRequest"])
@respx.mock
async def test_create_schedule_empty_read_back_is_rejected(service_type: str) -> None:
    """An accepted create that charted no slot must not read as a charted order."""
    _login()
    respx.get(MED).mock(return_value=httpx.Response(200, json=[]))
    post = respx.post(MED).mock(
        return_value=httpx.Response(200, json=_fixture("schedule_create_response.json"))
    )
    over: dict[str, Any] = {"service_type": service_type}
    if service_type != "MedicationRequest":
        over.update(first_day_slots=(), day_wise_slots=(), remaining_day_slots=())
    with pytest.raises(VendorRejected):
        await _client().create_medication_schedule(_schedule_write(**over))
    assert post.call_count == 1


@pytest.mark.parametrize("raw", [1772419260, 0, "not a date", "1999-12-31T23:59:00Z", {"x": 1}])
@respx.mock
async def test_administered_at_unparseable_or_implausible_is_rejected(raw: Any) -> None:
    """A given-at time in seconds would read as January 1970; refuse it, never guess."""
    _login()
    body = _fixture("medication_schedule.json")
    given = next(
        s for sched in body for s in sched["slots"] if s["medicationAdministration"] is not None
    )
    given["medicationAdministration"]["administeredDateTime"] = raw
    respx.get(MED).mock(return_value=httpx.Response(200, json=body))
    start = _utc(2026, 3, 2, 0, 0)
    with pytest.raises(VendorRejected):
        await _client().medication_schedule(PATIENT_A, start, start + timedelta(days=1))


@respx.mock
async def test_administration_without_given_at_reads_none() -> None:
    _login()
    body = _fixture("medication_schedule.json")
    given = next(
        s for sched in body for s in sched["slots"] if s["medicationAdministration"] is not None
    )
    given["medicationAdministration"]["administeredDateTime"] = None
    respx.get(MED).mock(return_value=httpx.Response(200, json=body))
    start = _utc(2026, 3, 2, 0, 0)
    slots = await _client().medication_schedule(PATIENT_A, start, start + timedelta(days=1))
    slot = next(s for s in slots if s.slot_uuid == given["uuid"])
    assert slot.administration_uuid is not None and slot.administered_at is None


@pytest.mark.parametrize("call", ["read", "pre_read", "post"])
@respx.mock
async def test_schedule_403_is_auth_error(call: str) -> None:
    _login()
    forbidden = httpx.Response(
        403, json={"error": {"message": "User doesn't have the following privilege(s)"}}
    )
    respx.get(MED).mock(return_value=forbidden if call != "post" else httpx.Response(200, json=[]))
    respx.post(MED).mock(return_value=forbidden)
    start = _utc(2026, 3, 2, 0, 0)
    with pytest.raises(AuthError):
        if call == "read":
            await _client().medication_schedule(PATIENT_A, start, start + timedelta(days=1))
        else:
            await _client().create_medication_schedule(_schedule_write())


@respx.mock
async def test_schedule_route_404_is_not_found() -> None:
    _login()
    respx.get(MED).mock(return_value=httpx.Response(404))
    start = _utc(2026, 3, 2, 0, 0)
    with pytest.raises(NotFound):
        await _client().medication_schedule(PATIENT_A, start, start + timedelta(days=1))


# ─── Medication chart: administrations ──────────────────────────────────────

SCHED_ADMIN = f"{REST}/ipd/scheduledMedicationAdministrations"
ADHOC_ADMIN = f"{REST}/ipd/adhocMedicationAdministrations"
SLOT_1 = "c1000000-0000-4000-8000-000000000001"
PRN_SLOT = "c2000000-0000-4000-8000-000000000001"
NEW_ADMIN = "d1000000-0000-4000-8000-000000000001"
OTHER_ADMIN = "d1000000-0000-4000-8000-000000000077"
MAR_REF = UUID("9c4e2b1a-7d3f-4a5b-8c6d-1e2f3a4b5c6d")
MAR_MARKER = "sm:" + str(MAR_REF)
GIVEN_AT = _utc(2026, 3, 2, 8, 51)


def _admin_write(**over: Any) -> MarAdministrationWrite:
    base: dict[str, Any] = {
        "patient_uuid": PATIENT_A,
        "visit_uuid": VISIT_A,
        "encounter_uuid": None,
        "order_uuid": ORDER_A,
        "slot_uuid": SLOT_1,
        "provider_uuids": (PROVIDER,),
        "status": "completed",
        "status_reason": None,
        "administered_at": GIVEN_AT,
        "dose": Decimal("1"),
        "dose_units": "Tablet(s)",
        "route": "Oral",
        "notes": "after food",
        "client_ref": MAR_REF,
    }
    base.update(over)
    return MarAdministrationWrite(**base)


def _prn_write(**over: Any) -> MarAdministrationWrite:
    return _admin_write(**{"slot_uuid": None, "notes": "fever above 38.5", **over})


def _admin_rep(uuid: str, status: str, notes: list[str]) -> dict[str, Any]:
    rep: dict[str, Any] = _fixture("administration_response.json")[0]
    rep["uuid"] = uuid
    rep["status"] = status
    rep["notes"] = [{"uuid": f"n-{i}", "author": None, "text": t} for i, t in enumerate(notes)]
    return rep


def _order_slots(
    admin: dict[str, Any] | None = None, status: str = "SCHEDULED", **order: Any
) -> list[dict[str, Any]]:
    """The order's scheduled slots; the first one is the slot being recorded."""
    body: list[dict[str, Any]] = _fixture("medication_schedule_by_order.json")
    body[0]["medicationAdministration"] = admin
    body[0]["status"] = status
    for slot in body:
        slot["order"].update(order)
    return body


def _placeholder(**order: Any) -> list[dict[str, Any]]:
    body = _order_slots(**order)[:1]
    body[0]["uuid"] = "c3000000-0000-4000-8000-000000000001"
    body[0]["serviceType"] = "AsNeededPlaceholder"
    return body


def _by_service_type(answers: dict[str, list[Any]]) -> Any:
    """GET side effect: answer each by-order read from a queue per serviceType."""

    def answer(request: httpx.Request) -> httpx.Response:
        kind = request.url.params["serviceType"]
        return httpx.Response(200, json=answers[kind].pop(0))

    return answer


def _refusal_code(exc: pytest.ExceptionInfo[VendorRejected]) -> str | None:
    return exc.value.code


@respx.mock
async def test_record_administration_body_and_marker() -> None:
    _login()
    given = _admin_rep(NEW_ADMIN, "completed", ["after food", MAR_MARKER])
    reads = respx.get(MED).mock(
        side_effect=_by_service_type(
            {"MEDICATION_REQUEST": [_order_slots(), _order_slots(given, "COMPLETED")]}
        )
    )
    post = respx.post(SCHED_ADMIN).mock(
        return_value=httpx.Response(200, json=_fixture("administration_response.json"))
    )
    out = await _client().record_administration(_admin_write())

    assert out == MarAdministrationOutcome(
        administration_uuid=NEW_ADMIN,
        slot_uuid=SLOT_1,
        created=True,
        status="completed",
        ref_matched=True,
    )
    assert dict(reads.calls[0].request.url.params) == {
        "patientUuid": PATIENT_A,
        "orderUuids": ORDER_A,
        "serviceType": "MEDICATION_REQUEST",
    }
    assert post.call_count == 1
    sent = json.loads(post.calls.last.request.content)
    assert sent == [
        {
            "patientUuid": PATIENT_A,
            "orderUuid": ORDER_A,
            "slotUuid": SLOT_1,
            "providers": [{"providerUuid": PROVIDER, "function": "Performer"}],
            "notes": [
                {"authorUuid": PROVIDER, "text": "after food"},
                {"authorUuid": PROVIDER, "text": MAR_MARKER},
            ],
            "status": "completed",
            # The request takes epoch SECONDS (the answer gives a java date in ms).
            "administeredDateTime": _epoch_s(GIVEN_AT),
            "dose": 1.0,
            "doseUnits": "Tablet(s)",
            "route": "Oral",
        }
    ]
    assert sent[0]["administeredDateTime"] == 1772441460
    # A request uuid makes the module return that administration instead of creating one.
    assert "uuid" not in sent[0]


@respx.mock
async def test_record_administration_names_encounter_when_given() -> None:
    _login()
    given = _admin_rep(NEW_ADMIN, "completed", [MAR_MARKER])
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {"MEDICATION_REQUEST": [_order_slots(), _order_slots(given, "COMPLETED")]}
        )
    )
    post = respx.post(SCHED_ADMIN).mock(
        return_value=httpx.Response(200, json=_fixture("administration_response.json"))
    )
    await _client().record_administration(_admin_write(encounter_uuid=VISIT_B, notes=None))
    sent = json.loads(post.calls.last.request.content)[0]
    assert sent["encounterUuid"] == VISIT_B
    assert sent["notes"] == [{"authorUuid": PROVIDER, "text": MAR_MARKER}]


@respx.mock
async def test_slot_already_recorded_returns_existing_not_created() -> None:
    """Someone else recorded the slot: report their administration, never "saved"."""
    _login()
    theirs = _admin_rep(OTHER_ADMIN, "not-done", ["patient vomiting"])
    respx.get(MED).mock(
        side_effect=_by_service_type({"MEDICATION_REQUEST": [_order_slots(theirs, "NOT_DONE")]})
    )
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[]))
    out = await _client().record_administration(_admin_write())
    assert out == MarAdministrationOutcome(
        administration_uuid=OTHER_ADMIN,
        slot_uuid=SLOT_1,
        created=False,
        status="not-done",
        ref_matched=False,
    )
    assert post.call_count == 0


@respx.mock
async def test_slot_recorded_by_this_ref_earlier_is_matched_not_created() -> None:
    """A retry after an unknown outcome finds its own earlier administration."""
    _login()
    ours = _admin_rep(NEW_ADMIN, "completed", [MAR_MARKER, "after food"])
    respx.get(MED).mock(
        side_effect=_by_service_type({"MEDICATION_REQUEST": [_order_slots(ours, "COMPLETED")]})
    )
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[]))
    out = await _client().record_administration(_admin_write())
    assert out.created is False and out.ref_matched is True
    assert out.administration_uuid == NEW_ADMIN
    assert post.call_count == 0


@respx.mock
async def test_slot_filled_between_read_and_post_reports_the_other_administration() -> None:
    """The module answers the slot's existing administration; the read-back shows whose."""
    _login()
    theirs = _admin_rep(OTHER_ADMIN, "completed", ["given early"])
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {"MEDICATION_REQUEST": [_order_slots(), _order_slots(theirs, "COMPLETED")]}
        )
    )
    respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[theirs]))
    out = await _client().record_administration(_admin_write())
    assert out == MarAdministrationOutcome(
        administration_uuid=OTHER_ADMIN,
        slot_uuid=SLOT_1,
        created=False,
        status="completed",
        ref_matched=False,
    )


@pytest.mark.parametrize(
    "answer",
    [
        [_admin_rep(OTHER_ADMIN, "completed", [MAR_MARKER])],  # names another administration
        {"uuid": NEW_ADMIN},  # not a list
        [],  # nothing answered
    ],
)
@respx.mock
async def test_post_answer_not_matching_slot_is_landed_rejection(answer: Any) -> None:
    _login()
    given = _admin_rep(NEW_ADMIN, "completed", [MAR_MARKER])
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {"MEDICATION_REQUEST": [_order_slots(), _order_slots(given, "COMPLETED")]}
        )
    )
    respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=answer))
    with pytest.raises(VendorRejected) as exc:
        await _client().record_administration(_admin_write())
    assert exc.value.landed is True


@respx.mock
async def test_read_back_slot_without_administration_is_landed_rejection() -> None:
    _login()
    respx.get(MED).mock(
        side_effect=_by_service_type({"MEDICATION_REQUEST": [_order_slots(), _order_slots()]})
    )
    respx.post(SCHED_ADMIN).mock(
        return_value=httpx.Response(200, json=_fixture("administration_response.json"))
    )
    with pytest.raises(VendorRejected) as exc:
        await _client().record_administration(_admin_write())
    assert exc.value.landed is True


@pytest.mark.parametrize("kind", ["scheduled", "adhoc"])
@respx.mock
async def test_unreadable_read_back_after_post_is_landed_rejection(kind: str) -> None:
    """Once the POST is accepted, any refusal reading the result says it may have landed."""
    _login()
    bad = _admin_rep(NEW_ADMIN, "on-hold", [MAR_MARKER])
    after = _fixture("medication_slots_as_needed.json")
    after[0]["medicationAdministration"] = bad
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {
                "MEDICATION_REQUEST": [_order_slots(), _order_slots(bad, "COMPLETED")],
                "AS_NEEDED_MEDICATION_REQUEST": [[], after],
                "AS_NEEDED_PLACEHOLDER": [_placeholder()],
            }
        )
    )
    answer = _fixture("administration_response.json")
    respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=answer))
    respx.post(ADHOC_ADMIN).mock(return_value=httpx.Response(200, json=answer[0]))
    with pytest.raises(VendorRejected) as exc:
        if kind == "scheduled":
            await _client().record_administration(_admin_write())
        else:
            await _client().record_adhoc_administration(_prn_write())
    assert exc.value.landed is True


@pytest.mark.parametrize("reason", [None, "", "   "])
@respx.mock
async def test_not_done_without_reason_is_value_error(reason: str | None) -> None:
    with pytest.raises(ValueError):
        await _client().record_administration(_admin_write(status="not-done", status_reason=reason))
    assert len(respx.calls) == 0


@respx.mock
async def test_not_done_reason_is_sent_and_kept_as_a_note() -> None:
    """The module drops statusReason when it saves; the reason survives as a note."""
    _login()
    skipped = _admin_rep(NEW_ADMIN, "not-done", ["Reason: patient refused", MAR_MARKER])
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {"MEDICATION_REQUEST": [_order_slots(), _order_slots(skipped, "NOT_DONE")]}
        )
    )
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[skipped]))
    out = await _client().record_administration(
        _admin_write(status="not-done", status_reason="patient refused", notes=None, dose=None)
    )
    sent = json.loads(post.calls.last.request.content)[0]
    assert sent["status"] == "not-done"
    assert sent["statusReason"] == "patient refused"
    assert sent["notes"] == [
        {"authorUuid": PROVIDER, "text": "Reason: patient refused"},
        {"authorUuid": PROVIDER, "text": MAR_MARKER},
    ]
    assert "dose" not in sent
    assert out.status == "not-done" and out.created is True


@pytest.mark.parametrize(
    ("slot_status", "order"),
    [
        ("STOPPED", {}),
        ("SCHEDULED", {"dateStopped": "2026-03-02T06:00:00.000+0000"}),
        ("SCHEDULED", {"action": "DISCONTINUE"}),
        ("SCHEDULED", "other_slot_stopped"),
    ],
)
@respx.mock
async def test_stopped_order_refused_order_not_active(slot_status: str, order: Any) -> None:
    _login()
    if order == "other_slot_stopped":
        body = _order_slots()
        body[1]["status"] = "STOPPED"
    else:
        body = _order_slots(status=slot_status, **order)
    respx.get(MED).mock(side_effect=_by_service_type({"MEDICATION_REQUEST": [body]}))
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(VendorRejected) as exc:
        await _client().record_administration(_admin_write())
    assert _refusal_code(exc) == "order_not_active"
    assert "order_not_active" in str(exc.value)
    assert post.call_count == 0


def _slots_with_one_dose_stopped(
    admin: dict[str, Any] | None = None, status: str = "SCHEDULED", **order: Any
) -> list[dict[str, Any]]:
    """The order's slots where the second one holds a dose recorded as ``stopped``."""
    body = _order_slots(admin, status, **order)
    body[1]["status"] = "STOPPED"
    body[1]["medicationAdministration"] = _admin_rep(OTHER_ADMIN, "stopped", ["infusion halted"])
    return body


@respx.mock
async def test_one_dose_recorded_as_stopped_does_not_stop_the_order() -> None:
    """A STOPPED slot holding an administration is one halted dose, not a stopped order."""
    _login()
    given = _admin_rep(NEW_ADMIN, "completed", [MAR_MARKER])
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {
                "MEDICATION_REQUEST": [
                    _slots_with_one_dose_stopped(),
                    _slots_with_one_dose_stopped(given, "COMPLETED"),
                ]
            }
        )
    )
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[given]))
    out = await _client().record_administration(_admin_write())
    assert post.call_count == 1
    assert out.created is True and out.administration_uuid == NEW_ADMIN


@pytest.mark.parametrize(
    "order", [{"dateStopped": "2026-03-02T06:00:00.000+0000"}, {"action": "DISCONTINUE"}]
)
@respx.mock
async def test_stopped_dose_beside_a_stopped_order_is_still_refused(order: Any) -> None:
    _login()
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {"MEDICATION_REQUEST": [_slots_with_one_dose_stopped(**order)]}
        )
    )
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(VendorRejected) as exc:
        await _client().record_administration(_admin_write())
    assert _refusal_code(exc) == "order_not_active"
    assert post.call_count == 0


@pytest.mark.parametrize(
    ("kind", "status", "error"),
    [
        ("scheduled", 500, TransientError),
        ("scheduled", 403, AuthError),
        ("adhoc", 500, TransientError),
        ("adhoc", 403, AuthError),
    ],
)
@respx.mock
async def test_read_back_error_after_post_is_marked_landed(
    kind: str, status: int, error: type[Exception]
) -> None:
    """The POST was accepted, so a failed read-back must not read as "not saved"."""
    _login()
    failed = httpx.Response(status, json={"error": {"message": "x"}})
    answer = _fixture("administration_response.json")
    if kind == "scheduled":
        respx.get(MED).mock(side_effect=[httpx.Response(200, json=_order_slots()), failed])
        respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=answer))
    else:
        respx.get(MED).mock(
            side_effect=[
                httpx.Response(200, json=[]),
                httpx.Response(200, json=_placeholder()),
                failed,
            ]
        )
        respx.post(ADHOC_ADMIN).mock(return_value=httpx.Response(200, json=answer[0]))
    with pytest.raises(error) as exc:
        if kind == "scheduled":
            await _client().record_administration(_admin_write())
        else:
            await _client().record_adhoc_administration(_prn_write())
    assert getattr(exc.value, "landed", None) is True
    if status == 500:
        assert getattr(exc.value, "status_code", None) == 500


@pytest.mark.parametrize("case", ["slot_not_on_order", "done_without_administration"])
@respx.mock
async def test_slot_not_recordable_is_slot_state_invalid(case: str) -> None:
    _login()
    on_order = case != "slot_not_on_order"
    body = _order_slots(status="COMPLETED") if on_order else _order_slots()[1:]
    respx.get(MED).mock(side_effect=_by_service_type({"MEDICATION_REQUEST": [body]}))
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(VendorRejected) as exc:
        await _client().record_administration(_admin_write())
    assert _refusal_code(exc) == "slot_state_invalid"
    assert post.call_count == 0


@respx.mock
async def test_missed_slot_may_still_be_recorded() -> None:
    _login()
    given = _admin_rep(NEW_ADMIN, "completed", [MAR_MARKER])
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {
                "MEDICATION_REQUEST": [
                    _order_slots(status="MISSED"),
                    _order_slots(given, "COMPLETED"),
                ]
            }
        )
    )
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(200, json=[given]))
    out = await _client().record_administration(_admin_write())
    assert out.created is True and post.call_count == 1


@pytest.mark.parametrize("status", ["given", "COMPLETED", "in-progress", "", "not_done"])
@respx.mock
async def test_status_codes_exact(status: str) -> None:
    with pytest.raises(ValueError):
        await _client().record_administration(_admin_write(status=status, status_reason="x"))
    with pytest.raises(ValueError):
        await _client().record_adhoc_administration(_prn_write(status=status, status_reason="x"))
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    "over",
    [
        {"slot_uuid": None},
        {"slot_uuid": "../x"},
        {"provider_uuids": ()},
        {"provider_uuids": ("../x",)},
        {"order_uuid": ""},
        {"patient_uuid": "a b"},
        {"encounter_uuid": "../x"},
        {"administered_at": datetime(2026, 3, 2, 8, 51)},
        {"dose": Decimal("-1")},
        {"dose": Decimal("NaN")},
        {"dose": 1.5},
        {"notes": "x" * 1001},
        {"dose_units": "x" * 256},
        {"route": ""},
        {"client_ref": str(MAR_REF)},
    ],
)
@respx.mock
async def test_record_administration_input_checked_before_io(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await _client().record_administration(_admin_write(**over))
    assert len(respx.calls) == 0


@respx.mock
async def test_adhoc_retry_finds_marker_and_does_not_post() -> None:
    _login()
    reads = respx.get(MED).mock(
        side_effect=_by_service_type(
            {"AS_NEEDED_MEDICATION_REQUEST": [_fixture("medication_slots_as_needed.json")]}
        )
    )
    post = respx.post(ADHOC_ADMIN).mock(return_value=httpx.Response(200, json={}))
    out = await _client().record_adhoc_administration(_prn_write())
    assert out == MarAdministrationOutcome(
        administration_uuid="d1000000-0000-4000-8000-000000000009",
        slot_uuid=PRN_SLOT,
        created=False,
        status="completed",
        ref_matched=True,
    )
    # The as-needed dose sits on an AS_NEEDED_MEDICATION_REQUEST slot of the order.
    assert dict(reads.calls[0].request.url.params) == {
        "patientUuid": PATIENT_A,
        "orderUuids": ORDER_A,
        "serviceType": "AS_NEEDED_MEDICATION_REQUEST",
    }
    assert post.call_count == 0


@respx.mock
async def test_adhoc_first_call_posts() -> None:
    _login()
    earlier = _fixture("medication_slots_as_needed.json")
    # An earlier dose of the same order by another write: not ours, so not a retry.
    earlier[0]["medicationAdministration"]["notes"] = [{"uuid": "n", "text": "sm:other"}]
    after = json.loads(json.dumps(earlier))
    new_slot = json.loads(json.dumps(_fixture("medication_slots_as_needed.json")[0]))
    new_slot["uuid"] = "c2000000-0000-4000-8000-000000000002"
    new_slot["medicationAdministration"]["uuid"] = NEW_ADMIN
    after.append(new_slot)
    reads = respx.get(MED).mock(
        side_effect=_by_service_type(
            {
                "AS_NEEDED_MEDICATION_REQUEST": [earlier, after],
                "AS_NEEDED_PLACEHOLDER": [_placeholder()],
            }
        )
    )
    post = respx.post(ADHOC_ADMIN).mock(
        return_value=httpx.Response(200, json=_fixture("administration_response.json")[0])
    )
    out = await _client().record_adhoc_administration(_prn_write())
    assert out == MarAdministrationOutcome(
        administration_uuid=NEW_ADMIN,
        slot_uuid="c2000000-0000-4000-8000-000000000002",
        created=True,
        status="completed",
        ref_matched=True,
    )
    assert reads.call_count == 3
    sent = json.loads(post.calls.last.request.content)
    assert sent == {
        "patientUuid": PATIENT_A,
        "orderUuid": ORDER_A,
        "providers": [{"providerUuid": PROVIDER, "function": "Performer"}],
        "notes": [
            {"authorUuid": PROVIDER, "text": "fever above 38.5"},
            {"authorUuid": PROVIDER, "text": MAR_MARKER},
        ],
        "status": "completed",
        "administeredDateTime": _epoch_s(GIVEN_AT),
        "dose": 1.0,
        "doseUnits": "Tablet(s)",
        "route": "Oral",
    }


@pytest.mark.parametrize(
    "order",
    [{"dateStopped": "2026-03-02T06:00:00.000+0000"}, {"action": "DISCONTINUE"}, "slot_stopped"],
)
@respx.mock
async def test_adhoc_stopped_order_refused_order_not_active(order: Any) -> None:
    _login()
    if order == "slot_stopped":
        placeholder = _placeholder()
        placeholder[0]["status"] = "STOPPED"
    else:
        placeholder = _placeholder(**order)
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {"AS_NEEDED_MEDICATION_REQUEST": [[]], "AS_NEEDED_PLACEHOLDER": [placeholder]}
        )
    )
    post = respx.post(ADHOC_ADMIN).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(VendorRejected) as exc:
        await _client().record_adhoc_administration(_prn_write())
    assert _refusal_code(exc) == "order_not_active"
    assert post.call_count == 0


@respx.mock
async def test_adhoc_earlier_dose_of_stopped_order_refuses_new_dose() -> None:
    _login()
    earlier = _fixture("medication_slots_as_needed.json")
    earlier[0]["medicationAdministration"]["notes"] = []
    earlier[0]["order"]["dateStopped"] = "2026-03-02T06:00:00.000+0000"
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {"AS_NEEDED_MEDICATION_REQUEST": [earlier], "AS_NEEDED_PLACEHOLDER": [[]]}
        )
    )
    post = respx.post(ADHOC_ADMIN).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(VendorRejected) as exc:
        await _client().record_adhoc_administration(_prn_write())
    assert _refusal_code(exc) == "order_not_active"
    assert post.call_count == 0


@pytest.mark.parametrize(
    "over",
    [
        {"slot_uuid": SLOT_1},
        {"status": "not-done", "status_reason": "refused"},
        {"status": "stopped"},
        {"provider_uuids": ()},
        {"visit_uuid": "../x"},
    ],
)
@respx.mock
async def test_adhoc_input_checked_before_io(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await _client().record_adhoc_administration(_prn_write(**over))
    assert len(respx.calls) == 0


@pytest.mark.parametrize("answer", [{}, [], {"uuid": OTHER_ADMIN}])
@respx.mock
async def test_adhoc_read_back_missing_is_landed_rejection(answer: Any) -> None:
    _login()
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {"AS_NEEDED_MEDICATION_REQUEST": [[], []], "AS_NEEDED_PLACEHOLDER": [_placeholder()]}
        )
    )
    respx.post(ADHOC_ADMIN).mock(return_value=httpx.Response(200, json=answer))
    with pytest.raises(VendorRejected) as exc:
        await _client().record_adhoc_administration(_prn_write())
    assert exc.value.landed is True


@respx.mock
async def test_adhoc_read_answering_another_service_type_is_rejected() -> None:
    """Scheduled slots in the answer mean the type filter was not applied."""
    _login()
    respx.get(MED).mock(
        side_effect=_by_service_type({"AS_NEEDED_MEDICATION_REQUEST": [_order_slots()]})
    )
    post = respx.post(ADHOC_ADMIN).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(VendorRejected):
        await _client().record_adhoc_administration(_prn_write())
    assert post.call_count == 0


@pytest.mark.parametrize("kind", ["scheduled", "adhoc"])
@respx.mock
async def test_administration_post_403_is_auth_error(kind: str) -> None:
    _login()
    forbidden = httpx.Response(
        403, json={"error": {"message": "User doesn't have the following privilege"}}
    )
    respx.get(MED).mock(
        side_effect=_by_service_type(
            {
                "MEDICATION_REQUEST": [_order_slots()],
                "AS_NEEDED_MEDICATION_REQUEST": [[]],
                "AS_NEEDED_PLACEHOLDER": [_placeholder()],
            }
        )
    )
    respx.post(SCHED_ADMIN).mock(return_value=forbidden)
    respx.post(ADHOC_ADMIN).mock(return_value=forbidden)
    with pytest.raises(AuthError):
        if kind == "scheduled":
            await _client().record_administration(_admin_write())
        else:
            await _client().record_adhoc_administration(_prn_write())


@respx.mock
async def test_administration_post_500_is_transient_not_retried() -> None:
    _login()
    respx.get(MED).mock(side_effect=_by_service_type({"MEDICATION_REQUEST": [_order_slots()]}))
    post = respx.post(SCHED_ADMIN).mock(return_value=httpx.Response(500))
    with pytest.raises(TransientError):
        await _client().record_administration(_admin_write())
    assert post.call_count == 1


def test_administration_write_requires_keywords() -> None:
    with pytest.raises(TypeError):
        MarAdministrationWrite(PATIENT_A)  # type: ignore[misc]
