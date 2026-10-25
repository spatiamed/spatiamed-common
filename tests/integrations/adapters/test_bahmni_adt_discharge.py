"""Discharge and end visit: DISCHARGE encounter (Bahmni frees the bed), then the visit ends."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import (
    ConfigError,
    ConflictError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.record_types import RecordAdtInput

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
DISCHARGE = f"{REST}/bahmnicore/discharge"
END_VISIT = f"{REST}/bahmnicore/visit/endVisit"

LOGIN_LOC = "3b1f6a0e-5d7c-4c1e-9a55-0f2f1b6d8c01"
ADT_LOC = "d9e8f7a6-b5c4-4d3e-8f2a-1b0c9d8e7f6a"
MARKER_CONCEPT = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e5f"
NOTES_CONCEPT = "a7b8c9d0-e1f2-4a3b-8c4d-5e6f7a8b9c0d"
IPD_TYPE = "4f3e2d1c-0b9a-4876-9543-210fedcba987"
ADMISSION_TYPE = "6b7c8d9e-0f1a-4b2c-9d3e-4f5a6b7c8d9e"
DISCHARGE_TYPE = "f0ae16b9-aae6-429f-bb4b-cc591351037a"
PATIENT = "849e3827-09bb-4123-a07b-3fd63801d0d5"
OTHER_PATIENT = "2e4c6a8b-0d1f-4a3c-9e5b-7d9f1b3d5f7a"
VISIT = "7d0c6659-716d-45db-ac0f-c5343098a934"
PROVIDER = "8057f2e0-839d-4b61-a030-b226f64a816a"
ADMIT_ENC = "452878b8-1c08-42ce-84d6-c5d33744a5d1"
ENC = "8d4f4a73-89a3-4c00-943c-dbe23eab8ddd"
BED_UUID = "46dc5ad0-8385-4426-8e3b-350a4b0c9ad8"
EVENT = UUID("fba2b0df-ca6e-4dbf-8003-b3dc8982ef60")
OTHER_EVENT = UUID("5f0b8e3a-2c47-4d19-b6a1-93e2d7c40f58")
MARKER = f"sm:adt:{EVENT}"
AT = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)
STOPPED = "2026-10-03T11:00:00.000+0530"

CFG: dict[str, Any] = {
    "location_uuid": LOGIN_LOC,
    "identifier_type_uuid": "T",
    "identifier_source_uuid": "S",
    "note_marker_concept_uuid": MARKER_CONCEPT,
    "ipd_visit_type_uuid": IPD_TYPE,
    "admission_encounter_type_uuid": ADMISSION_TYPE,
    "discharge_encounter_type_uuid": DISCHARGE_TYPE,
    "concept_map": {"adt_notes": NOTES_CONCEPT},
}

DIS = RecordAdtInput(
    event_id=EVENT,
    patient_uuid=PATIENT,
    provider_uuid=PROVIDER,
    location_uuid=ADT_LOC,
    bed_id=12,
    bed_uuid=BED_UUID,
    at=AT,
    visit_uuid=VISIT,
)


def _client(**overrides: Any) -> BahmniRecordClient:
    return BahmniRecordClient(
        BahmniHttp(BASE, "u", "p"), BahmniConfig.from_mapping({**CFG, **overrides})
    )


def _enc(uuid: str, type_uuid: str, *markers: str) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "encounterType": {"uuid": type_uuid},
        "obs": [{"concept": {"uuid": MARKER_CONCEPT}, "value": m} for m in markers],
    }


class FakeBahmni:
    """One IPD visit; the discharge POST files a DISCHARGE encounter, endVisit stops the visit."""

    def __init__(self) -> None:
        self.visit: dict[str, Any] = {
            "uuid": VISIT,
            "stopDatetime": None,
            "patient": {"uuid": PATIENT},
            "visitType": {"uuid": IPD_TYPE},
            "encounters": [_enc(ADMIT_ENC, ADMISSION_TYPE, f"sm:adt:{OTHER_EVENT}")],
        }
        self.discharge_response: dict[str, Any] | None = None
        self.posts: list[tuple[str, dict[str, Any] | None]] = []

    def _visit_one(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.visit)

    def _discharge(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.posts.append(("discharge", body))
        markers = [o["value"] for o in body["observations"]]
        self.visit["encounters"].append(_enc(ENC, body["encounterTypeUuid"], *markers))
        response = self.discharge_response or {"encounterUuid": ENC, "visitUuid": VISIT}
        return httpx.Response(200, json=response)

    def _end_visit(self, request: httpx.Request) -> httpx.Response:
        self.posts.append((f"endVisit?{request.url.params['visitUuid']}", None))
        self.visit["stopDatetime"] = STOPPED
        return httpx.Response(200, json={"uuid": VISIT, "stopDateTime": STOPPED})

    def install(self) -> None:
        respx.get(f"{REST}/session").mock(
            return_value=httpx.Response(
                200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
            )
        )
        respx.get(f"{REST}/visit/{VISIT}").mock(side_effect=self._visit_one)
        respx.post(DISCHARGE).mock(side_effect=self._discharge)
        respx.post(END_VISIT).mock(side_effect=self._end_visit)

    def kinds(self) -> list[str]:
        return [k for k, _ in self.posts]


def _marker_obs() -> dict[str, Any]:
    return {"concept": {"uuid": MARKER_CONCEPT}, "value": MARKER}


# ─── Discharge ──────────────────────────────────────────────────────────────


@respx.mock
async def test_discharge_ends_visit_and_is_retry_safe() -> None:
    fake = FakeBahmni()
    fake.install()
    first = await _client().discharge(DIS)
    assert fake.kinds() == ["discharge", f"endVisit?{VISIT}"]
    assert (first.encounter_uuid, first.created) == (ENC, True)

    # A retry with the same event (e.g. the caller lost the response) writes nothing.
    again = await _client().discharge(DIS)
    assert (again.encounter_uuid, again.created) == (ENC, False)
    assert fake.kinds() == ["discharge", f"endVisit?{VISIT}"]


@respx.mock
async def test_discharge_marker_found_skips_post() -> None:
    # The DISCHARGE landed but endVisit did not: the retry only ends the visit.
    fake = FakeBahmni()
    fake.visit["encounters"].append(_enc(ENC, DISCHARGE_TYPE, MARKER))
    fake.install()
    result = await _client().discharge(DIS)
    assert fake.kinds() == [f"endVisit?{VISIT}"]
    assert (result.encounter_uuid, result.created) == (ENC, False)


@respx.mock
async def test_discharge_retry_after_visit_ended_is_success() -> None:
    fake = FakeBahmni()
    fake.visit["encounters"].append(_enc(ENC, DISCHARGE_TYPE, MARKER))
    fake.visit["stopDatetime"] = STOPPED
    fake.install()
    result = await _client().discharge(DIS)
    assert (result.encounter_uuid, result.created) == (ENC, False)
    assert fake.posts == []
    # One visit read: the stop state already read decides there is nothing to end.
    assert sum(1 for c in respx.calls if c.request.url.path.endswith(VISIT)) == 1


@respx.mock
async def test_discharge_no_marker_stopped_visit_conflict() -> None:
    fake = FakeBahmni()
    fake.visit["stopDatetime"] = STOPPED
    fake.install()
    with pytest.raises(ConflictError, match="^visit_closed$"):
        await _client().discharge(DIS)
    assert fake.posts == []


@respx.mock
async def test_discharge_marker_on_another_encounter_type_is_not_ours() -> None:
    fake = FakeBahmni()
    fake.visit["encounters"].append(_enc(ADMIT_ENC, ADMISSION_TYPE, MARKER))
    fake.install()
    result = await _client().discharge(DIS)
    assert result.created is True
    assert fake.kinds() == ["discharge", f"endVisit?{VISIT}"]


@respx.mock
async def test_discharge_marker_on_voided_encounter_is_not_ours() -> None:
    # An undone discharge (voided DISCHARGE) is not this event's landed write.
    fake = FakeBahmni()
    fake.visit["encounters"].append({**_enc(ADMIT_ENC, DISCHARGE_TYPE, MARKER), "voided": True})
    fake.install()
    result = await _client().discharge(DIS)
    assert (result.encounter_uuid, result.created) == (ENC, True)
    assert fake.kinds() == ["discharge", f"endVisit?{VISIT}"]
    rep = next(c.request for c in respx.calls if c.request.url.path.endswith(VISIT)).url.params["v"]
    assert "encounters:(uuid,voided," in rep and "))," not in rep


@respx.mock
async def test_discharge_other_patients_visit_value_error() -> None:
    fake = FakeBahmni()
    fake.visit["patient"] = {"uuid": OTHER_PATIENT}
    fake.install()
    with pytest.raises(ValueError, match="another patient"):
        await _client().discharge(DIS)
    assert fake.posts == []


@respx.mock
async def test_discharge_payload_carries_visit_uuid_and_no_encounter_datetime() -> None:
    fake = FakeBahmni()
    fake.install()
    await _client().discharge(DIS)
    (_, body), _ = fake.posts
    assert body == {
        "patientUuid": PATIENT,
        "visitUuid": VISIT,
        "encounterTypeUuid": DISCHARGE_TYPE,
        "visitTypeUuid": IPD_TYPE,
        "locationUuid": ADT_LOC,
        "providers": [{"uuid": PROVIDER}],
        "observations": [_marker_obs()],
    }
    # Bahmni stamps "now"; a past time would take its retrospective path.
    assert "encounterDateTime" not in body


@respx.mock
async def test_discharge_note_sent_by_concept_uuid() -> None:
    fake = FakeBahmni()
    fake.install()
    await _client().discharge(replace(DIS, note="Discharged home, stable"))
    (_, body), _ = fake.posts
    assert body is not None
    assert body["observations"] == [
        _marker_obs(),
        {"concept": {"uuid": NOTES_CONCEPT}, "value": "Discharged home, stable"},
    ]


@respx.mock
async def test_discharge_without_end_visit_leaves_visit_open() -> None:
    fake = FakeBahmni()
    fake.install()
    result = await _client().discharge(DIS, end_visit=False)
    assert fake.kinds() == ["discharge"]
    assert result.created is True and fake.visit["stopDatetime"] is None


@respx.mock
async def test_discharge_ignores_bed_fields() -> None:
    # Bahmni frees the patient's bed itself; the bed fields are not sent or checked.
    fake = FakeBahmni()
    fake.install()
    await _client().discharge(replace(DIS, bed_id=0, bed_uuid=""))
    (_, body), _ = fake.posts
    assert body is not None and not any("bed" in k.lower() for k in body)
    assert not any("/bed" in str(c.request.url) for c in respx.calls)


@respx.mock
async def test_discharge_without_visit_uuid_value_error() -> None:
    with pytest.raises(ValueError, match="visit_uuid"):
        await _client().discharge(replace(DIS, visit_uuid=None))
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"patient_uuid": "../session"}, "patient_uuid"),
        ({"visit_uuid": "x?y=1"}, "visit_uuid"),
    ],
)
@respx.mock
async def test_discharge_rejects_malformed_args_before_http(
    change: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValueError, match=field):
        await _client().discharge(replace(DIS, **change))
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    "missing", ["discharge_encounter_type_uuid", "ipd_visit_type_uuid", "note_marker_concept_uuid"]
)
@respx.mock
async def test_discharge_missing_config_error_before_http(missing: str) -> None:
    with pytest.raises(ConfigError, match=missing):
        await _client(**{missing: ""}).discharge(DIS)
    assert len(respx.calls) == 0


@respx.mock
async def test_discharge_note_without_concept_config_error() -> None:
    with pytest.raises(ConfigError, match="adt_notes"):
        await _client(concept_map={}).discharge(replace(DIS, note="x"))
    assert len(respx.calls) == 0


@respx.mock
async def test_discharge_route_404_not_found() -> None:
    fake = FakeBahmni()
    fake.install()
    respx.post(DISCHARGE).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound, match="discharge"):
        await _client().discharge(DIS)
    assert not any(c.request.url.path.endswith("endVisit") for c in respx.calls)


@respx.mock
async def test_discharge_response_without_encounter_uuid_landed() -> None:
    fake = FakeBahmni()
    fake.discharge_response = {"visitUuid": VISIT}
    fake.install()
    with pytest.raises(VendorRejected) as err:
        await _client().discharge(DIS)
    assert err.value.landed is True
    assert fake.kinds() == ["discharge"]


@respx.mock
async def test_discharge_end_visit_failure_then_retry_ends_visit() -> None:
    fake = FakeBahmni()
    fake.install()
    respx.post(END_VISIT).mock(return_value=httpx.Response(500))
    with pytest.raises(TransientError):
        await _client().discharge(DIS)
    respx.post(END_VISIT).mock(side_effect=fake._end_visit)
    result = await _client().discharge(DIS)
    assert result.created is False
    assert fake.kinds() == ["discharge", f"endVisit?{VISIT}"]
    assert fake.visit["stopDatetime"] == STOPPED


# ─── End visit ──────────────────────────────────────────────────────────────


@respx.mock
async def test_end_visit_open_visit_posts_and_returns_true() -> None:
    fake = FakeBahmni()
    fake.install()
    assert await _client().end_visit(VISIT) is True
    assert fake.kinds() == [f"endVisit?{VISIT}"]
    (post,) = [c.request for c in respx.calls if c.request.method == "POST"]
    assert post.content == b""


@respx.mock
async def test_end_visit_already_stopped_false_no_post() -> None:
    # Bahmni's endVisit on a stopped visit overwrites its stop time with "now".
    fake = FakeBahmni()
    fake.visit["stopDatetime"] = STOPPED
    fake.install()
    assert await _client().end_visit(VISIT) is False
    assert fake.posts == []


@respx.mock
async def test_end_visit_unknown_visit_not_found_no_post() -> None:
    fake = FakeBahmni()
    fake.install()
    respx.get(f"{REST}/visit/{VISIT}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().end_visit(VISIT)
    assert fake.posts == []


@respx.mock
async def test_end_visit_route_404_not_found() -> None:
    fake = FakeBahmni()
    fake.install()
    respx.post(END_VISIT).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound, match="endVisit"):
        await _client().end_visit(VISIT)


@respx.mock
async def test_end_visit_rejects_malformed_uuid_before_http() -> None:
    with pytest.raises(ValueError, match="visit_uuid"):
        await _client().end_visit("a/b")
    assert len(respx.calls) == 0
