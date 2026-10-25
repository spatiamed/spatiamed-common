"""Admit and transfer: ADT encounter + bed assignment, retry-safe on the event marker."""

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
from sm_common.integrations.exceptions import ConfigError, ConflictError, NotFound, TransientError
from sm_common.integrations.record_types import RecordAdtInput, RecordBedAssignment

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
ENCOUNTER = f"{REST}/bahmnicore/bahmniencounter"
END_AND_CREATE = f"{REST}/bahmnicore/visit/endVisitAndCreateEncounter"

LOGIN_LOC = "3b1f6a0e-5d7c-4c1e-9a55-0f2f1b6d8c01"
ADT_LOC = "d9e8f7a6-b5c4-4d3e-8f2a-1b0c9d8e7f6a"
WARD = "08ed3779-ac52-4fa4-8e93-686892cf4527"
MARKER_CONCEPT = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e5f"
NOTES_CONCEPT = "a7b8c9d0-e1f2-4a3b-8c4d-5e6f7a8b9c0d"
IPD_TYPE = "4f3e2d1c-0b9a-4876-9543-210fedcba987"
OPD_TYPE = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
ADMISSION_TYPE = "6b7c8d9e-0f1a-4b2c-9d3e-4f5a6b7c8d9e"
TRANSFER_TYPE = "9c8b7a6f-5e4d-4c3b-8a2f-1e0d9c8b7a6f"
CONSULT_TYPE = "e1f2a3b4-c5d6-4e7f-9a0b-1c2d3e4f5a6b"
PATIENT = "849e3827-09bb-4123-a07b-3fd63801d0d5"
OTHER_PATIENT = "2e4c6a8b-0d1f-4a3c-9e5b-7d9f1b3d5f7a"
VISIT = "7d0c6659-716d-45db-ac0f-c5343098a934"
NEW_VISIT = "c3d4e5f6-a7b8-4c9d-8e0f-1a2b3c4d5e6f"
OTHER_VISIT = "b4e9c1d2-7a3f-4e85-9c06-1d2f3a4b5c6d"
PROVIDER = "8057f2e0-839d-4b61-a030-b226f64a816a"
ENC = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"
SHARED_ENC = "0f1e2d3c-4b5a-4968-8776-655443322110"
BED_ID = 12
BED_UUID = "46dc5ad0-8385-4426-8e3b-350a4b0c9ad8"
OLD_BED_ID = 11
OLD_BED_UUID = "560544a8-97e7-4b10-b84a-f72dbac4561e"
NEW_BED_UUID = "e0b7c3d1-6f2a-4c58-9d14-7a3e5b9c2f60"
EVENT = UUID("366e01c5-ecaa-45e9-8244-898a3d1279e1")
OTHER_EVENT = UUID("5f0b8e3a-2c47-4d19-b6a1-93e2d7c40f58")
MARKER = f"sm:adt:{EVENT}"
AT = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)

CFG: dict[str, Any] = {
    "location_uuid": LOGIN_LOC,
    "identifier_type_uuid": "T",
    "identifier_source_uuid": "S",
    "note_marker_concept_uuid": MARKER_CONCEPT,
    "ipd_visit_type_uuid": IPD_TYPE,
    "admission_encounter_type_uuid": ADMISSION_TYPE,
    "transfer_encounter_type_uuid": TRANSFER_TYPE,
    "concept_map": {"adt_notes": NOTES_CONCEPT},
}

ADMIT = RecordAdtInput(
    event_id=EVENT,
    patient_uuid=PATIENT,
    provider_uuid=PROVIDER,
    location_uuid=ADT_LOC,
    bed_id=BED_ID,
    bed_uuid=BED_UUID,
    at=AT,
)
TRANSFER = replace(ADMIT, visit_uuid=VISIT)


def _client(**overrides: Any) -> BahmniRecordClient:
    cfg = {**CFG, **overrides}
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig.from_mapping(cfg))


def _enc(uuid: str, type_uuid: str, *markers: str) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "encounterType": {"uuid": type_uuid},
        "obs": [{"concept": {"uuid": MARKER_CONCEPT}, "value": m} for m in markers],
    }


def _bed_layout(
    bed_id: int, bed_uuid: str, number: str, patients: list[str], status: str | None = None
) -> dict[str, Any]:
    return {
        "rowNumber": 1,
        "columnNumber": bed_id - 10,
        "bedNumber": number,
        "bedId": bed_id,
        "bedUuid": bed_uuid,
        "status": status or ("OCCUPIED" if patients else "AVAILABLE"),
        "bedType": {"name": "general", "displayName": "General Bed"},
        "patients": [{"uuid": p} for p in patients],
    }


class FakeBahmni:
    """Bahmni as respx routes; every test edits the state it needs before ``install``."""

    def __init__(self) -> None:
        # One visit per uuid: what GET visit/<uuid>?v=custom:(...) returns.
        self.visits: dict[str, dict[str, Any]] = {
            VISIT: {
                "uuid": VISIT,
                "startDatetime": "2026-10-01T09:00:00.000+0530",
                "stopDatetime": None,
                "patient": {"uuid": PATIENT},
                "visitType": {"uuid": OPD_TYPE, "display": "OPD"},
                "location": {"uuid": LOGIN_LOC},
                "encounters": [],
            }
        }
        self.bed_of_patient: int | None = None  # bedId Bahmni holds the patient in
        # Bed BED_ID as Bahmni reports it: "OCCUPIED" = held by another patient.
        self.bed_status = "AVAILABLE"
        # bedmanagement 6.0.0 rewrites a bed's uuid whenever a patient leaves it, so the
        # uuid Bahmni holds now can differ from the one the caller stored.
        self.bed_uuid_now = BED_UUID
        # Layout status of BED_ID when it should not follow its patient list (Bahmni does
        # not lock a bed, so the two can disagree).
        self.bed_layout_status: str | None = None
        self.encounter_response: dict[str, Any] = {"encounterUuid": ENC, "visitUuid": VISIT}
        self.posts: list[tuple[str, dict[str, Any] | None]] = []

    # ─── handlers ────────────────────────────────────────────────────────────

    def _visit_list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["patient"] == PATIENT
        rows = [
            {k: v for k, v in visit.items() if k != "encounters"} for visit in self.visits.values()
        ]
        if params.get("includeInactive") == "false":
            rows = [r for r in rows if r["stopDatetime"] is None]
        return httpx.Response(200, json={"results": rows})

    def _visit_one(self, request: httpx.Request) -> httpx.Response:
        uuid = request.url.path.rsplit("/", 1)[-1]
        visit = self.visits.get(uuid)
        if visit is None:
            return httpx.Response(404, json={"error": {"message": "Object not found"}})
        return httpx.Response(200, json=visit)

    def _start_visit(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.posts.append(("visit", body))
        self.visits[NEW_VISIT] = {
            "uuid": NEW_VISIT,
            "startDatetime": "2026-10-01T10:00:00.000+0530",
            "stopDatetime": None,
            "patient": {"uuid": PATIENT},
            "visitType": {"uuid": body["visitType"], "display": "IPD"},
            "location": {"uuid": LOGIN_LOC},
            "encounters": [],
        }
        return httpx.Response(201, json=self.visits[NEW_VISIT])

    def _beds_by_patient(self, request: httpx.Request) -> httpx.Response:
        if self.bed_of_patient is None:
            return httpx.Response(200, json={"results": []})
        number = f"GW-{self.bed_of_patient}"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "bedId": self.bed_of_patient,
                        "bedNumber": number,
                        "bedType": None,
                        "physicalLocation": {"uuid": WARD, "display": "General Ward"},
                        "patients": [{"uuid": PATIENT}],
                    }
                ]
            },
        )

    def _on_bed(self, bed_id: int) -> list[str]:
        if self.bed_of_patient == bed_id:
            return [PATIENT]
        if bed_id == BED_ID and self.bed_status == "OCCUPIED":
            return [OTHER_PATIENT]
        return []

    def _ward(self, request: httpx.Request) -> httpx.Response:
        layouts = [
            _bed_layout(11, OLD_BED_UUID, "GW-11", self._on_bed(11)),
            _bed_layout(12, self.bed_uuid_now, "GW-12", self._on_bed(12), self.bed_layout_status),
        ]
        return httpx.Response(
            200,
            json={
                "ward": {"uuid": WARD, "display": "General Ward"},
                "totalBeds": 2,
                "occupiedBeds": sum(1 for b in layouts if b["patients"]),
                "bedLayouts": layouts,
            },
        )

    def _bed_details(self, request: httpx.Request) -> httpx.Response:
        # Bahmni's `beds/<id>` (BedDetails): no status, no uuid; the patients on the bed.
        bed_id = int(request.url.path.rsplit("/", 1)[-1])
        if bed_id not in (11, 12):
            return httpx.Response(404, json={"error": {"message": "Object not found"}})
        return httpx.Response(
            200,
            json={
                "bedId": bed_id,
                "bedNumber": f"GW-{bed_id}",
                "bedType": {"name": "general", "displayName": "General Bed"},
                "physicalLocation": {"uuid": WARD, "display": "General Ward"},
                "patients": [{"uuid": p} for p in self._on_bed(bed_id)],
            },
        )

    def _encounter(self, request: httpx.Request) -> httpx.Response:
        self.posts.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json=self.encounter_response)

    def _assign(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bed_id = int(request.url.path.rsplit("/", 1)[-1])
        self.posts.append((f"beds/{bed_id}", body))
        return httpx.Response(
            200,
            json={
                "bedId": bed_id,
                "bedNumber": f"GW-{bed_id}",
                "bedType": {"name": "general", "displayName": "General Bed"},
                "physicalLocation": {"uuid": WARD, "display": "General Ward"},
                "patients": [{"uuid": body["patientUuid"]}],
            },
        )

    def install(self) -> None:
        respx.get(f"{REST}/session").mock(
            return_value=httpx.Response(
                200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
            )
        )
        respx.get(f"{REST}/visit").mock(side_effect=self._visit_list)
        respx.post(f"{REST}/visit").mock(side_effect=self._start_visit)
        respx.get(url__regex=rf"{REST}/visit/[^/?]+").mock(side_effect=self._visit_one)
        respx.get(f"{REST}/beds").mock(side_effect=self._beds_by_patient)
        respx.get(f"{REST}/admissionLocation/{WARD}").mock(side_effect=self._ward)
        respx.post(ENCOUNTER).mock(side_effect=self._encounter)
        respx.post(END_AND_CREATE).mock(side_effect=self._encounter)
        respx.post(url__regex=rf"{REST}/beds/\d+").mock(side_effect=self._assign)
        respx.get(url__regex=rf"{REST}/beds/\d+$").mock(side_effect=self._bed_details)

    def ipd_visit_with(self, *encounters: dict[str, Any]) -> None:
        self.visits[VISIT]["visitType"] = {"uuid": IPD_TYPE, "display": "IPD"}
        self.visits[VISIT]["encounters"] = list(encounters)

    def encounter_posts(self) -> list[tuple[str, dict[str, Any] | None]]:
        return [p for p in self.posts if "bahmnicore" in p[0]]

    def bed_posts(self) -> list[tuple[str, dict[str, Any] | None]]:
        return [p for p in self.posts if p[0].startswith("beds/")]


def _posts() -> list[httpx.Request]:
    return [c.request for c in respx.calls if c.request.method == "POST"]


def _marker_obs() -> dict[str, Any]:
    return {"concept": {"uuid": MARKER_CONCEPT}, "value": MARKER}


# ─── Admit: which visit ─────────────────────────────────────────────────────


@respx.mock
async def test_admit_new_ipd_visit_uses_end_visit_and_create() -> None:
    fake = FakeBahmni()
    fake.encounter_response = {"encounterUuid": ENC, "visitUuid": NEW_VISIT}
    fake.install()
    result = await _client().admit(ADMIT)

    (url, body), (bed_url, bed_body) = fake.posts
    assert url.startswith(END_AND_CREATE)
    assert httpx.URL(url).params["visitUuid"] == VISIT
    assert body == {
        "patientUuid": PATIENT,
        "encounterTypeUuid": ADMISSION_TYPE,
        "visitTypeUuid": IPD_TYPE,
        "locationUuid": ADT_LOC,
        "providers": [{"uuid": PROVIDER}],
        "observations": [_marker_obs()],
    }
    # Bahmni attaches an encounter to a visitUuid even after ending that visit.
    assert "visitUuid" not in body
    assert (bed_url, bed_body) == (f"beds/{BED_ID}", {"patientUuid": PATIENT, "encounterUuid": ENC})
    assert result.visit_uuid == NEW_VISIT
    assert result.encounter_uuid == ENC
    assert result.created_encounter is True
    assert result.new_visit is True
    assert result.bed == RecordBedAssignment(
        bed_id=BED_ID,
        bed_uuid=BED_UUID,
        bed_number="GW-12",
        bed_type="general",
        ward_uuid=WARD,
        ward_name="General Ward",
    )


@respx.mock
async def test_admit_into_open_visit_when_flag_false() -> None:
    fake = FakeBahmni()
    fake.install()
    result = await _client(admit_new_ipd_visit=False).admit(ADMIT)

    (url, body), _ = fake.posts
    assert url == ENCOUNTER
    assert body is not None and body["visitUuid"] == VISIT
    assert not any(r.url.path.endswith("endVisitAndCreateEncounter") for r in _posts())
    assert (result.visit_uuid, result.new_visit, result.created_encounter) == (VISIT, False, True)


@respx.mock
async def test_admit_into_open_ipd_visit_never_converts() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with()
    fake.install()
    result = await _client().admit(ADMIT)
    (url, body), _ = fake.posts
    assert url == ENCOUNTER
    assert body is not None and body["visitUuid"] == VISIT
    assert result.new_visit is False


@respx.mock
async def test_admit_starts_ipd_visit_when_none_open() -> None:
    fake = FakeBahmni()
    fake.visits[VISIT]["stopDatetime"] = "2026-09-30T18:00:00.000+0530"
    fake.encounter_response = {"encounterUuid": ENC, "visitUuid": NEW_VISIT}
    fake.install()
    result = await _client().admit(ADMIT)

    kinds = [p[0] for p in fake.posts]
    assert kinds == ["visit", ENCOUNTER, f"beds/{BED_ID}"]
    assert fake.posts[0][1] == {"patient": PATIENT, "visitType": IPD_TYPE, "location": LOGIN_LOC}
    enc_body = fake.posts[1][1]
    assert enc_body is not None and enc_body["visitUuid"] == NEW_VISIT
    assert (result.visit_uuid, result.new_visit) == (NEW_VISIT, True)


@respx.mock
async def test_admit_visit_changed_conflict() -> None:
    fake = FakeBahmni()
    fake.install()
    with pytest.raises(ConflictError, match="^visit_changed$"):
        await _client().admit(replace(ADMIT, visit_uuid=OTHER_VISIT))
    assert _posts() == []


@respx.mock
async def test_admit_with_matching_visit_uuid_proceeds() -> None:
    fake = FakeBahmni()
    fake.install()
    result = await _client(admit_new_ipd_visit=False).admit(replace(ADMIT, visit_uuid=VISIT))
    assert result.visit_uuid == VISIT


# ─── Admit: the bed ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("open_visit", [True, False])
@respx.mock
async def test_assign_refuses_occupied_bed(open_visit: bool) -> None:
    fake = FakeBahmni()
    fake.bed_status = "OCCUPIED"
    if not open_visit:
        fake.visits[VISIT]["stopDatetime"] = "2026-09-30T18:00:00.000+0530"
    fake.install()
    with pytest.raises(ConflictError, match="^bed_occupied$"):
        await _client().admit(ADMIT)
    # Nothing written: no visit, no encounter, no bed assignment.
    assert _posts() == []


@respx.mock
async def test_transfer_refuses_occupied_bed() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with()
    fake.bed_of_patient = OLD_BED_ID
    fake.bed_status = "OCCUPIED"
    fake.install()
    with pytest.raises(ConflictError, match="^bed_occupied$"):
        await _client().transfer(TRANSFER)
    assert _posts() == []


# ─── Admit: retries ─────────────────────────────────────────────────────────


@respx.mock
async def test_admit_retry_after_bed_failure_reassigns_only() -> None:
    fake = FakeBahmni()
    # The first attempt converted the visit and wrote the ADMISSION, then the bed call failed.
    # Other events' markers (and a bare-uuid note marker) share the encounter.
    fake.ipd_visit_with(
        _enc(SHARED_ENC, ADMISSION_TYPE, f"sm:adt:{OTHER_EVENT}", str(EVENT), MARKER),
    )
    fake.install()
    result = await _client().admit(ADMIT)

    assert fake.encounter_posts() == []
    assert fake.bed_posts() == [
        (f"beds/{BED_ID}", {"patientUuid": PATIENT, "encounterUuid": SHARED_ENC})
    ]
    assert result.encounter_uuid == SHARED_ENC
    assert (result.created_encounter, result.new_visit) == (False, False)


@respx.mock
async def test_admit_retry_when_bed_already_assigned_no_writes() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with(_enc(ENC, ADMISSION_TYPE, MARKER))
    fake.bed_of_patient = BED_ID
    fake.install()
    result = await _client().admit(ADMIT)

    assert _posts() == []
    assert result.bed.bed_id == BED_ID and result.bed.bed_uuid == BED_UUID
    assert (result.encounter_uuid, result.created_encounter) == (ENC, False)


@respx.mock
async def test_admit_marker_on_another_encounter_type_is_not_ours() -> None:
    fake = FakeBahmni()
    # Same marker value on a consultation encounter: not an ADMISSION, so admit writes.
    fake.ipd_visit_with(_enc(SHARED_ENC, CONSULT_TYPE, MARKER))
    fake.install()
    result = await _client().admit(ADMIT)
    assert len(fake.encounter_posts()) == 1
    assert result.created_encounter is True


@respx.mock
async def test_admit_marker_in_newest_closed_visit_when_none_open() -> None:
    fake = FakeBahmni()
    # The converted IPD visit was closed (e.g. a stale-visit job) before the bed retry.
    fake.ipd_visit_with(_enc(ENC, ADMISSION_TYPE, MARKER))
    fake.visits[VISIT]["stopDatetime"] = "2026-10-02T00:00:00.000+0530"
    fake.install()
    result = await _client().admit(ADMIT)
    assert [p[0] for p in fake.posts] == [f"beds/{BED_ID}"]
    assert (result.visit_uuid, result.encounter_uuid, result.created_encounter) == (
        VISIT,
        ENC,
        False,
    )


@respx.mock
async def test_admission_encounter_merge_returns_shared_uuid() -> None:
    fake = FakeBahmni()
    # An ADMISSION by the same provider inside the session window: Bahmni merges into it.
    fake.ipd_visit_with(_enc(SHARED_ENC, ADMISSION_TYPE, f"sm:adt:{OTHER_EVENT}"))
    fake.encounter_response = {"encounterUuid": SHARED_ENC, "visitUuid": VISIT}
    fake.install()
    first = await _client().admit(ADMIT)
    assert (first.encounter_uuid, first.created_encounter) == (SHARED_ENC, True)
    assert fake.bed_posts()[0][1] == {"patientUuid": PATIENT, "encounterUuid": SHARED_ENC}

    # The merged encounter now carries our marker too; a retry finds it by marker.
    fake.visits[VISIT]["encounters"][0]["obs"].append(_marker_obs())
    fake.bed_of_patient = BED_ID
    again = await _client().admit(ADMIT)
    assert (again.encounter_uuid, again.created_encounter) == (SHARED_ENC, False)
    assert len(fake.encounter_posts()) == 1


# ─── Transfer ───────────────────────────────────────────────────────────────


@respx.mock
async def test_transfer_posts_transfer_encounter_then_bed() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with(_enc(SHARED_ENC, ADMISSION_TYPE, f"sm:adt:{OTHER_EVENT}"))
    fake.bed_of_patient = OLD_BED_ID
    fake.install()
    result = await _client().transfer(TRANSFER)

    (url, body), (bed_url, bed_body) = fake.posts
    assert url == ENCOUNTER
    assert body is not None and body["encounterTypeUuid"] == TRANSFER_TYPE
    # Bahmni unassigns the old bed itself when assigning the new one.
    assert (bed_url, bed_body) == (f"beds/{BED_ID}", {"patientUuid": PATIENT, "encounterUuid": ENC})
    assert not any(r.method == "DELETE" for r in (c.request for c in respx.calls))
    assert (result.visit_uuid, result.created_encounter, result.new_visit) == (VISIT, True, False)
    assert result.bed.bed_id == BED_ID


@respx.mock
async def test_transfer_retry_finds_transfer_marker() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with(
        _enc(SHARED_ENC, ADMISSION_TYPE, MARKER),  # wrong type for a transfer
        _enc(ENC, TRANSFER_TYPE, MARKER),
    )
    fake.bed_of_patient = OLD_BED_ID
    fake.install()
    result = await _client().transfer(TRANSFER)
    assert fake.encounter_posts() == []
    assert fake.bed_posts() == [(f"beds/{BED_ID}", {"patientUuid": PATIENT, "encounterUuid": ENC})]
    assert result.created_encounter is False


@respx.mock
async def test_transfer_into_closed_visit_conflict() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with()
    fake.visits[VISIT]["stopDatetime"] = "2026-10-02T00:00:00.000+0530"
    fake.install()
    with pytest.raises(ConflictError, match="^visit_closed$"):
        await _client().transfer(TRANSFER)
    assert _posts() == []


@respx.mock
async def test_transfer_visit_of_another_patient_value_error() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with()
    fake.visits[VISIT]["patient"] = {"uuid": OTHER_PATIENT}
    fake.install()
    with pytest.raises(ValueError, match="another patient"):
        await _client().transfer(TRANSFER)
    assert _posts() == []


@respx.mock
async def test_transfer_without_visit_uuid_value_error() -> None:
    with pytest.raises(ValueError, match="visit_uuid"):
        await _client().transfer(ADMIT)
    assert len(respx.calls) == 0


# ─── Payloads ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["admit", "transfer"])
@respx.mock
async def test_adt_payload_carries_visit_uuid(kind: str) -> None:
    fake = FakeBahmni()
    if kind == "transfer":
        fake.ipd_visit_with()
        fake.bed_of_patient = OLD_BED_ID
    fake.install()
    client = _client(admit_new_ipd_visit=False)
    if kind == "admit":
        await client.admit(ADMIT)
    else:
        await client.transfer(TRANSFER)
    (url, body), _ = fake.posts
    assert url == ENCOUNTER
    assert body == {
        "patientUuid": PATIENT,
        "visitUuid": VISIT,
        "encounterTypeUuid": ADMISSION_TYPE if kind == "admit" else TRANSFER_TYPE,
        "visitTypeUuid": IPD_TYPE,
        "locationUuid": ADT_LOC,
        "providers": [{"uuid": PROVIDER}],
        "observations": [_marker_obs()],
    }


@pytest.mark.parametrize("kind", ["admit", "transfer"])
@respx.mock
async def test_adt_note_sent_by_concept_uuid(kind: str) -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with()
    fake.install()
    a = replace(TRANSFER, note="Shifted for monitoring")
    await (_client().admit(a) if kind == "admit" else _client().transfer(a))
    (_, body), _ = fake.posts
    assert body is not None
    assert body["observations"] == [
        _marker_obs(),
        {"concept": {"uuid": NOTES_CONCEPT}, "value": "Shifted for monitoring"},
    ]


@pytest.mark.parametrize("kind", ["admit", "transfer"])
@respx.mock
async def test_adt_note_without_concept_config_error(kind: str) -> None:
    a = replace(TRANSFER, note="Shifted for monitoring")
    client = _client(concept_map={})
    with pytest.raises(ConfigError, match="adt_notes"):
        await (client.admit(a) if kind == "admit" else client.transfer(a))
    assert len(respx.calls) == 0


# ─── Config and arguments, before any HTTP ──────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "missing"),
    [
        ("admit", "admission_encounter_type_uuid"),
        ("admit", "ipd_visit_type_uuid"),
        ("admit", "note_marker_concept_uuid"),
        ("transfer", "transfer_encounter_type_uuid"),
        ("transfer", "ipd_visit_type_uuid"),
        ("transfer", "note_marker_concept_uuid"),
    ],
)
@respx.mock
async def test_admit_missing_encounter_type_config_error(kind: str, missing: str) -> None:
    client = _client(**{missing: ""})
    with pytest.raises(ConfigError, match=missing):
        await (client.admit(TRANSFER) if kind == "admit" else client.transfer(TRANSFER))
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"patient_uuid": "../session"}, "patient_uuid"),
        ({"bed_uuid": "a/b"}, "bed_uuid"),
        ({"visit_uuid": "x?y=1"}, "visit_uuid"),
        ({"bed_id": 0}, "bed_id"),
        ({"bed_id": True}, "bed_id"),
    ],
)
@pytest.mark.parametrize("kind", ["admit", "transfer"])
@respx.mock
async def test_adt_rejects_malformed_args_before_http(
    kind: str, change: dict[str, Any], field: str
) -> None:
    a = replace(TRANSFER, **change)
    with pytest.raises(ValueError, match=field):
        await (_client().admit(a) if kind == "admit" else _client().transfer(a))
    assert len(respx.calls) == 0


# ─── Routes ─────────────────────────────────────────────────────────────────


@respx.mock
async def test_end_visit_and_create_route_404_not_found() -> None:
    fake = FakeBahmni()
    fake.install()
    respx.post(END_AND_CREATE).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().admit(ADMIT)
    assert fake.bed_posts() == []


@respx.mock
async def test_bed_assign_route_404_not_found() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with(_enc(ENC, ADMISSION_TYPE, MARKER))
    fake.install()
    respx.post(url__regex=rf"{REST}/beds/\d+").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().admit(ADMIT)


# ─── Fix round 1 ────────────────────────────────────────────────────────────


@respx.mock
async def test_admit_caller_visit_closed_none_open_conflict() -> None:
    fake = FakeBahmni()
    # The caller saw VISIT open; it has since closed and nothing else is open.
    fake.visits[VISIT]["stopDatetime"] = "2026-10-02T00:00:00.000+0530"
    fake.install()
    with pytest.raises(ConflictError, match="^visit_changed$"):
        await _client().admit(replace(ADMIT, visit_uuid=VISIT))
    assert _posts() == []


@pytest.mark.parametrize("resubmit_visit", [None, NEW_VISIT])
@respx.mock
async def test_admit_retry_after_started_visit_then_encounter_failure(
    resubmit_visit: str | None,
) -> None:
    fake = FakeBahmni()
    fake.visits[VISIT]["stopDatetime"] = "2026-09-30T18:00:00.000+0530"
    fake.encounter_response = {"encounterUuid": ENC, "visitUuid": NEW_VISIT}
    fake.install()
    route = respx.post(ENCOUNTER).mock(return_value=httpx.Response(500))
    with pytest.raises(TransientError):
        await _client().admit(ADMIT)
    assert [p[0] for p in fake.posts] == ["visit"]

    # The caller re-reads the open visit (the started IPD one) and resubmits the event.
    route.mock(side_effect=fake._encounter)
    result = await _client().admit(replace(ADMIT, visit_uuid=resubmit_visit))
    assert [p[0] for p in fake.posts] == ["visit", ENCOUNTER, f"beds/{BED_ID}"]
    enc_body = fake.posts[1][1]
    assert enc_body is not None and enc_body["visitUuid"] == NEW_VISIT
    assert (result.visit_uuid, result.created_encounter) == (NEW_VISIT, True)


@pytest.mark.parametrize("kind", ["admit", "transfer"])
@respx.mock
async def test_adt_without_marker_into_current_bed_conflict(kind: str) -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with()
    fake.bed_of_patient = BED_ID
    fake.install()
    client = _client()
    with pytest.raises(ConflictError, match="^already_in_bed$"):
        await (client.admit(ADMIT) if kind == "admit" else client.transfer(TRANSFER))
    assert _posts() == []


@respx.mock
async def test_admit_result_reports_visit_stopped() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with(_enc(ENC, ADMISSION_TYPE, MARKER))
    fake.visits[VISIT]["stopDatetime"] = "2026-10-02T00:00:00.000+0530"
    fake.install()
    stale = await _client().admit(ADMIT)
    assert stale.visit_stopped is True

    fake.visits[VISIT]["stopDatetime"] = None
    fresh = await _client().admit(ADMIT)
    assert fresh.visit_stopped is False


@respx.mock
async def test_transfer_result_visit_not_stopped() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with()
    fake.bed_of_patient = OLD_BED_ID
    fake.install()
    assert (await _client().transfer(TRANSFER)).visit_stopped is False


# ─── Bed identity: bedmanagement 6.0.0 rewrites a bed's uuid on unassign ────
# Freeing a bed (transfer out, discharge, DELETE beds/<id>) saves a Bed built from a
# projection of only (id, bed number): a fresh object carries a random uuid, so the
# row's uuid changes. bed_id is the only durable key; bed_uuid is informational.


@respx.mock
async def test_admit_after_bed_uuid_rewritten_reads_bed_by_id() -> None:
    fake = FakeBahmni()
    fake.bed_uuid_now = NEW_BED_UUID
    fake.install()
    result = await _client().admit(ADMIT)

    assert fake.bed_posts() == [(f"beds/{BED_ID}", {"patientUuid": PATIENT, "encounterUuid": ENC})]
    # The assignment carries the uuid Bahmni holds now, not the caller's stale one.
    assert result.bed.bed_uuid == NEW_BED_UUID
    assert not any(c.request.url.path.startswith("/openmrs/ws/rest/v1/bed/") for c in respx.calls)


@respx.mock
async def test_admit_retry_in_bed_with_stale_bed_uuid_no_writes() -> None:
    fake = FakeBahmni()
    fake.ipd_visit_with(_enc(ENC, ADMISSION_TYPE, MARKER))
    fake.bed_of_patient = BED_ID
    fake.bed_uuid_now = NEW_BED_UUID
    fake.install()
    result = await _client().admit(ADMIT)

    assert _posts() == []
    assert (result.bed.bed_id, result.bed.bed_uuid) == (BED_ID, NEW_BED_UUID)


@respx.mock
async def test_admit_unknown_bed_id_not_found_before_any_write() -> None:
    fake = FakeBahmni()
    fake.install()
    with pytest.raises(NotFound, match="bed 99"):
        await _client().admit(replace(ADMIT, bed_id=99))
    assert _posts() == []


@respx.mock
async def test_admit_refuses_available_bed_that_lists_another_patient() -> None:
    # Bahmni does not lock a bed to one patient: the layout can say AVAILABLE while still
    # listing a patient on the bed. That bed is not free.
    fake = FakeBahmni()
    fake.bed_status = "OCCUPIED"  # another patient on BED_ID
    fake.bed_layout_status = "AVAILABLE"
    fake.install()
    with pytest.raises(ConflictError, match="^bed_occupied$"):
        await _client().admit(ADMIT)
    assert _posts() == []


@pytest.mark.parametrize("kind", ["admit", "transfer"])
@respx.mock
async def test_adt_without_bed_uuid_goes_by_bed_id(kind: str) -> None:
    # The caller may hold only the durable bed id (Bahmni rewrites bed uuids).
    fake = FakeBahmni()
    fake.ipd_visit_with()
    fake.bed_uuid_now = NEW_BED_UUID
    fake.install()
    client = _client()
    a = replace(ADMIT, bed_uuid=None)
    result = await (
        client.admit(a) if kind == "admit" else client.transfer(replace(a, visit_uuid=VISIT))
    )
    assert fake.bed_posts() == [(f"beds/{BED_ID}", {"patientUuid": PATIENT, "encounterUuid": ENC})]
    assert result.bed.bed_uuid == NEW_BED_UUID
