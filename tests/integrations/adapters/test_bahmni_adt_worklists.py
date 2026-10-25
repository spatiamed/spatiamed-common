import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import (
    EncounterOutsideVisit,
    AuthError,
    ConfigError,
    NotFound,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.record_types import RecordAdtRow, RecordEncounterResult

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
SQL = f"{REST}/bahmnicore/sql"
WARDS = f"{REST}/admissionLocation"
ENCOUNTER = f"{REST}/bahmnicore/bahmniencounter"

LOGIN_LOC = "3b1f6a0e-5d7c-4c1e-9a55-0f2f1b6d8c01"
VISIT_LOC = "a6c2d4e8-1f3b-4a5c-8d7e-9b0a1c2d3e4f"
MARKER_CONCEPT = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e5f"
CONSULT_TYPE = "e1f2a3b4-c5d6-4e7f-9a0b-1c2d3e4f5a6b"
NOTE_CONCEPT = "f2a3b4c5-d6e7-4f8a-9b0c-2d3e4f5a6b7c"
PATIENT = "849e3827-09bb-4123-a07b-3fd63801d0d5"
VISIT = "7d0c6659-716d-45db-ac0f-c5343098a934"
OTHER_VISIT = "b4e9c1d2-7a3f-4e85-9c06-1d2f3a4b5c6d"
PROVIDER = "8057f2e0-839d-4b61-a030-b226f64a816a"
EVENT = UUID("366e01c5-ecaa-45e9-8244-898a3d1279e1")
OTHER_EVENT = UUID("5f0b8e3a-2c47-4d19-b6a1-93e2d7c40f58")
AT = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)
AT_MS = 1790829000000

ADT_CFG: dict[str, Any] = {
    "location_uuid": LOGIN_LOC,
    "identifier_type_uuid": "T",
    "identifier_source_uuid": "S",
    "visit_location_uuid": VISIT_LOC,
    "note_marker_concept_uuid": MARKER_CONCEPT,
    "consultation_encounter_type_uuid": CONSULT_TYPE,
    "disposition_codes": {"admit": "Admit Patient", "discharge": "Discharge Patient"},
    "concept_map": {"disposition_note": NOTE_CONCEPT},
}


def _client(**overrides: Any) -> BahmniRecordClient:
    cfg = {**ADT_CFG, **overrides}
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig.from_mapping(cfg))


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _row(**extra: Any) -> dict[str, Any]:
    # Bahmni returns one object per SQL row keyed by column label; NULL comes back as "".
    return {
        "name": "Test Patient",
        "identifier": "TEST100001",
        "uuid": PATIENT,
        "activeVisitUuid": VISIT,
        **extra,
    }


def _echo(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"encounterUuid": json.loads(req.content)["encounterUuid"]})


def _visit_with(*encounters: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"uuid": VISIT, "encounters": list(encounters)})


# ─── Worklists ──────────────────────────────────────────────────────────────


@respx.mock
async def test_worklist_uses_visit_location() -> None:
    _login()
    route = respx.get(SQL).mock(return_value=httpx.Response(200, json=[_row()]))
    rows = await _client().adt_worklist("to_admit")
    params = route.calls[0].request.url.params
    assert params["q"] == "emrapi.sqlSearch.patientsToAdmit"
    # The visit location, never the login location.
    assert params["location_uuid"] == VISIT_LOC
    assert params["v"] == "full"
    assert [r.patient_uuid for r in rows] == [PATIENT]


@pytest.mark.parametrize(
    ("which", "query"),
    [
        ("admitted", "emrapi.sqlSearch.admittedPatients"),
        ("to_discharge", "emrapi.sqlSearch.patientsToDischarge"),
    ],
)
@respx.mock
async def test_worklist_query_names(which: Any, query: str) -> None:
    _login()
    route = respx.get(SQL).mock(return_value=httpx.Response(200, json=[_row()]))
    await _client().adt_worklist(which)
    assert route.calls[0].request.url.params["q"] == query


@respx.mock
async def test_worklist_admitted_empty_with_occupied_beds_logs_location_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _login()
    respx.get(SQL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(WARDS).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"ward": {"uuid": VISIT, "display": "W"}, "totalBeds": 4, "occupiedBeds": 2}
                ]
            },
        )
    )
    with caplog.at_level(logging.WARNING):
        assert await _client().adt_worklist("admitted") == []
    assert any("adt_location_mismatch" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_worklist_admitted_empty_with_no_occupied_beds_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _login()
    respx.get(SQL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(WARDS).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"ward": {"uuid": VISIT, "display": "W"}, "totalBeds": 4, "occupiedBeds": 0}
                ]
            },
        )
    )
    with caplog.at_level(logging.WARNING):
        assert await _client().adt_worklist("admitted") == []
    assert not any("adt_location_mismatch" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_worklist_mismatch_check_lets_auth_errors_through() -> None:
    _login()
    respx.get(SQL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(WARDS).mock(return_value=httpx.Response(403))
    with pytest.raises(AuthError):
        await _client().adt_worklist("admitted")


@respx.mock
async def test_worklist_route_404_not_found() -> None:
    # Never "no patients": an empty list is the normal answer for a quiet ward.
    _login()
    respx.get(SQL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().adt_worklist("to_admit")


@respx.mock
async def test_worklist_dedupes_patient_visit_rows() -> None:
    # Bahmni's stock SQL repeats a patient per identifier or person name.
    _login()
    respx.get(SQL).mock(
        return_value=httpx.Response(
            200,
            json=[_row(), _row(identifier="TEST100002"), _row(activeVisitUuid=OTHER_VISIT)],
        )
    )
    rows = await _client().adt_worklist("admitted")
    assert [(r.identifier, r.visit_uuid) for r in rows] == [
        ("TEST100001", VISIT),
        ("TEST100001", OTHER_VISIT),
    ]


@respx.mock
async def test_worklist_empty_to_discharge_does_not_probe_wards() -> None:
    _login()
    respx.get(SQL).mock(return_value=httpx.Response(200, json=[]))
    wards = respx.get(WARDS).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await _client().adt_worklist("to_discharge") == []
    assert wards.call_count == 0


@respx.mock
async def test_worklist_maps_known_columns_and_extra() -> None:
    _login()
    respx.get(SQL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _row(hasBeenAdmitted="true", ward="General Ward", bed="GW-11", advisedAt=AT_MS),
                _row(uuid="46dc5ad0-8385-4426-8e3b-350a4b0c9ad8", ward="", bed="", advisedAt=""),
            ],
        )
    )
    rows = await _client().adt_worklist("admitted")
    assert rows[0] == RecordAdtRow(
        patient_uuid=PATIENT,
        identifier="TEST100001",
        name="Test Patient",
        visit_uuid=VISIT,
        ward="General Ward",
        bed_number="GW-11",
        advised_at=AT,
        extra={"hasBeenAdmitted": "true"},
    )
    # SQL NULL arrives as "": optional columns read as None, never "".
    assert (rows[1].ward, rows[1].bed_number, rows[1].advised_at, rows[1].extra) == (
        None,
        None,
        None,
        {},
    )


@respx.mock
async def test_worklist_malformed_is_loud() -> None:
    _login()
    respx.get(SQL).mock(return_value=httpx.Response(200, json={"results": []}))
    with pytest.raises(VendorRejected):
        await _client().adt_worklist("to_admit")
    respx.get(SQL).mock(return_value=httpx.Response(200, json=[_row(activeVisitUuid="")]))
    with pytest.raises(VendorRejected):
        await _client().adt_worklist("to_admit")


async def test_worklist_missing_visit_location_raises_before_http() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ConfigError, match="visit_location_uuid"):
            await _client(visit_location_uuid="").adt_worklist("to_admit")
        assert mock.calls.call_count == 0


# ─── Dispositions ───────────────────────────────────────────────────────────


async def _dispose(
    client: BahmniRecordClient, kind: Any, note: str | None
) -> RecordEncounterResult:
    return await client.write_disposition(
        event_id=EVENT,
        patient_uuid=PATIENT,
        visit_uuid=VISIT,
        provider_uuid=PROVIDER,
        kind=kind,
        at=AT,
        note=note,
    )


@respx.mock
async def test_write_disposition_payload_exact() -> None:
    _login()
    lookup = respx.get(f"{REST}/visit/{VISIT}").mock(return_value=_visit_with())
    post = respx.post(ENCOUNTER).mock(side_effect=_echo)
    result = await _dispose(_client(), "discharge", "Stable, review in 7 days")
    assert result == RecordEncounterResult(encounter_uuid=str(EVENT), created=True)
    assert "obs:(concept:(uuid),value)" in lookup.calls[0].request.url.params["v"]
    assert json.loads(post.calls[0].request.content) == {
        # Its own encounter: a uuid-less write fails once two candidates exist.
        "encounterUuid": str(EVENT),
        "patientUuid": PATIENT,
        "visitUuid": VISIT,
        "locationUuid": LOGIN_LOC,
        "encounterTypeUuid": CONSULT_TYPE,
        "encounterDateTime": AT_MS,
        "providers": [{"uuid": PROVIDER}],
        "observations": [{"concept": {"uuid": MARKER_CONCEPT}, "value": f"sm:adt:{EVENT}"}],
        "disposition": {
            "code": "DISCHARGE",
            "conceptName": "Discharge Patient",
            "dispositionDateTime": AT_MS,
            # Bahmni resolves additionalObs concepts by uuid only.
            "additionalObs": [
                {"concept": {"uuid": NOTE_CONCEPT}, "value": "Stable, review in 7 days"}
            ],
        },
    }


@respx.mock
async def test_write_disposition_admit_without_note() -> None:
    _login()
    respx.get(f"{REST}/visit/{VISIT}").mock(return_value=_visit_with())
    post = respx.post(ENCOUNTER).mock(side_effect=_echo)
    await _dispose(_client(concept_map={}), "admit", None)
    disposition = json.loads(post.calls[0].request.content)["disposition"]
    assert disposition["code"] == "ADMIT"
    assert disposition["conceptName"] == "Admit Patient"
    assert disposition["additionalObs"] == []


@respx.mock
async def test_write_disposition_idempotent_on_marker() -> None:
    _login()
    respx.get(f"{REST}/visit/{VISIT}").mock(
        return_value=_visit_with(
            {
                "uuid": "enc-other",
                "obs": [
                    {"concept": {"uuid": MARKER_CONCEPT}, "value": f"sm:adt:{OTHER_EVENT}"},
                    # An older note marker is the bare id.
                    {"concept": {"uuid": MARKER_CONCEPT}, "value": str(EVENT)},
                ],
            },
            {
                "uuid": "enc-merged",
                "obs": [
                    {"concept": {"uuid": NOTE_CONCEPT}, "value": "note"},
                    # Another event merged into the same encounter.
                    {"concept": {"uuid": MARKER_CONCEPT}, "value": f"sm:adt:{OTHER_EVENT}"},
                    {"concept": {"uuid": MARKER_CONCEPT}, "value": f"sm:adt:{EVENT}"},
                ],
            },
        )
    )
    post = respx.post(ENCOUNTER).mock(return_value=httpx.Response(200, json={}))
    result = await _dispose(_client(), "discharge", None)
    assert result == RecordEncounterResult(encounter_uuid="enc-merged", created=False)
    assert post.call_count == 0


@pytest.mark.parametrize(
    ("overrides", "kind", "note", "key"),
    [
        ({"disposition_codes": {"admit": "Admit Patient"}}, "discharge", None, "discharge"),
        ({"note_marker_concept_uuid": ""}, "admit", None, "note_marker_concept_uuid"),
        ({"consultation_encounter_type_uuid": ""}, "admit", None, "consultation"),
        ({"concept_map": {}}, "admit", "a note", "disposition_note"),
    ],
)
async def test_disposition_missing_config_raises_before_http(
    overrides: dict[str, Any], kind: Any, note: str | None, key: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ConfigError, match=key):
            await _dispose(_client(**overrides), kind, note)
        assert mock.calls.call_count == 0


@respx.mock
async def test_write_disposition_route_404_and_no_uuid() -> None:
    _login()
    respx.get(f"{REST}/visit/{VISIT}").mock(return_value=_visit_with())
    respx.post(ENCOUNTER).mock(return_value=httpx.Response(404))
    with pytest.raises(WriteNotSupported):
        await _dispose(_client(), "admit", None)
    respx.post(ENCOUNTER).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(VendorRejected) as exc:
        await _dispose(_client(), "admit", None)
    assert exc.value.landed is True


async def test_write_disposition_rejects_malformed_visit_uuid_before_http() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="visit_uuid"):
            await _client().write_disposition(
                event_id=EVENT,
                patient_uuid=PATIENT,
                visit_uuid="v/../session",
                provider_uuid=PROVIDER,
                kind="admit",
                at=AT,
                note=None,
            )
        assert mock.calls.call_count == 0


@respx.mock
async def test_write_disposition_caller_encounter_uuid_and_range() -> None:
    _login()
    respx.get(f"{REST}/visit/{VISIT}").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": VISIT,
                "startDatetime": "2026-10-01T10:00:00.000+0530",  # == AT
                "stopDatetime": None,
                "encounters": [],
            },
        )
    )
    post = respx.post(ENCOUNTER).mock(side_effect=_echo)
    given = UUID("4f6a1c2e-8b3d-4e5f-9a0b-1c2d3e4f5a6b")
    client = _client()
    r = await client.write_disposition(
        event_id=EVENT,
        patient_uuid=PATIENT,
        visit_uuid=VISIT,
        provider_uuid=PROVIDER,
        kind="admit",
        at=AT,
        note=None,
        encounter_uuid=given,
    )
    assert r.encounter_uuid == str(given)
    assert json.loads(post.calls[0].request.content)["encounterUuid"] == str(given)
    with pytest.raises(EncounterOutsideVisit):
        await client.write_disposition(
            event_id=EVENT,
            patient_uuid=PATIENT,
            visit_uuid=VISIT,
            provider_uuid=PROVIDER,
            kind="admit",
            at=AT - timedelta(seconds=1),
            note=None,
        )
    assert post.call_count == 1
