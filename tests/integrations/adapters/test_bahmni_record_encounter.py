import json
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.adapters.bahmni_record import _ENCOUNTER_REP, _MARKER_REP
from sm_common.integrations.exceptions import ConfigError, NotFound, VendorRejected
from sm_common.integrations.record_types import (
    RecordDiagnosis,
    RecordDrugOrder,
    RecordEncounterInput,
    RecordObs,
)

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
VISIT = f"{REST}/visit"
ENC_LIST = f"{REST}/encounter"
WRITE = f"{REST}/bahmnicore/bahmniencounter"
NOTE = UUID("0b1c2d3e-0000-4000-8000-00000000beef")
AT = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)
AT_MS = 1790829000000
FLEX = "org.openmrs.module.bahmniemrapi.drugorder.dosinginstructions.FlexibleDosingInstructions"


def _client(**over: object) -> BahmniRecordClient:
    base: dict[str, object] = {
        "location_uuid": "loc-1",
        "identifier_type_uuid": "idtype",
        "identifier_source_uuid": "idsrc",
        "opd_visit_type_uuid": "vt-opd",
        "consultation_encounter_type_uuid": "et-consult",
        "note_marker_concept_uuid": "c-marker",
        "drug_other_concept_uuid": "c-drug-other",
        "drug_quantity_units": "Tablet",
        "concept_map": {"chief_complaint": "c-cc", "vitals_pulse": "c-pulse"},
    }
    base.update(over)
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig(**base))  # type: ignore[arg-type]


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _visit(uuid: str = "v-1", stop: str | None = None) -> dict:  # type: ignore[type-arg]
    return {
        "uuid": uuid,
        "visitType": {"uuid": "vt-opd", "display": "OPD"},
        "startDatetime": "2026-10-01T10:00:00.000+0530",
        "stopDatetime": stop,
        "location": {"uuid": "loc-1"},
    }


def _input(**over: object) -> RecordEncounterInput:
    base: dict[str, object] = {
        "note_id": NOTE,
        "patient_uuid": "p-1",
        "visit_uuid": "v-1",
        "provider_uuid": "pr-1",
        "encounter_at": AT,
        "obs": [RecordObs("chief_complaint", "Cough"), RecordObs("vitals_pulse", 88.0)],
        "diagnoses": [RecordDiagnosis(code="CA40.Z", title="Pneumonia", primary=True)],
        "drug_orders": [
            RecordDrugOrder(
                name="Amoxicillin 500mg",
                dose="1 tab",
                frequency="TDS",
                duration_days=5,
                instructions="after food",
            )
        ],
    }
    base.update(over)
    return RecordEncounterInput(**base)  # type: ignore[arg-type]


def _encounters(rows: list, visit: dict | None = None) -> respx.Route:  # type: ignore[type-arg]
    """GET visit/v-1: the encounter-bearing rep returns rows, any other rep the visit."""

    def answer(req: httpx.Request) -> httpx.Response:
        if "encounters:(" in req.url.params.get("v", ""):
            return httpx.Response(200, json={"uuid": "v-1", "encounters": rows})
        return httpx.Response(200, json=visit or _visit())

    return respx.get(f"{VISIT}/v-1").mock(side_effect=answer)


def _no_existing() -> respx.Route:
    return _encounters([])


# ─── visits ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_active_visit_asks_for_active_only_and_parses():
    _login()
    route = respx.get(VISIT).mock(return_value=httpx.Response(200, json={"results": [_visit()]}))
    v = await _client().active_visit("p-1")
    q = route.calls[0].request.url.params
    assert q["patient"] == "p-1" and q["includeInactive"] == "false"
    assert q["v"] == "custom:(uuid,visitType,startDatetime,stopDatetime,location)"
    assert v is not None
    assert (v.uuid, v.visit_type, v.location_uuid, v.stop) == ("v-1", "OPD", "loc-1", None)
    assert v.start == datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)


@respx.mock
async def test_start_visit_reuses_active():
    _login()
    respx.get(VISIT).mock(return_value=httpx.Response(200, json={"results": [_visit()]}))
    post = respx.post(VISIT).mock(return_value=httpx.Response(201, json=_visit("v-2")))
    v, created = await _client().start_visit("p-1")
    assert (v.uuid, created) == ("v-1", False)
    assert post.call_count == 0


@respx.mock
async def test_start_visit_creates_with_config_type():
    _login()
    respx.get(VISIT).mock(return_value=httpx.Response(200, json={"results": []}))
    post = respx.post(VISIT).mock(return_value=httpx.Response(201, json=_visit("v-2")))
    v, created = await _client().start_visit("p-1")
    assert (v.uuid, created) == ("v-2", True)
    assert json.loads(post.calls[0].request.content) == {
        "patient": "p-1",
        "visitType": "vt-opd",
        "location": "loc-1",
    }


@respx.mock
async def test_start_visit_explicit_type_overrides_config():
    _login()
    respx.get(VISIT).mock(return_value=httpx.Response(200, json={"results": []}))
    post = respx.post(VISIT).mock(return_value=httpx.Response(201, json=_visit("v-2")))
    await _client().start_visit("p-1", visit_type_uuid="vt-other")
    assert json.loads(post.calls[0].request.content)["visitType"] == "vt-other"


@respx.mock
async def test_start_visit_without_any_type_raises_before_post():
    _login()
    respx.get(VISIT).mock(return_value=httpx.Response(200, json={"results": []}))
    post = respx.post(VISIT).mock(return_value=httpx.Response(201, json=_visit("v-2")))
    with pytest.raises(ConfigError):
        await _client(opd_visit_type_uuid="").start_visit("p-1")
    assert post.call_count == 0


@respx.mock
async def test_list_visits_includes_inactive_newest_first():
    _login()
    older = _visit("v-old", stop="2026-09-01T11:00:00.000+0530")
    older["startDatetime"] = "2026-09-01T10:00:00.000+0530"
    route = respx.get(VISIT).mock(
        return_value=httpx.Response(200, json={"results": [older, _visit()]})
    )
    visits = await _client().list_visits("p-1", limit=5)
    q = route.calls[0].request.url.params
    assert q["includeInactive"] == "true" and q["limit"] == "5"
    assert [v.uuid for v in visits] == ["v-1", "v-old"]
    assert visits[1].stop is not None


# ─── get_visit ───────────────────────────────────────────────────────────────

ENC_ROW = {
    "uuid": "e-1",
    "encounterType": {"display": "Consultation"},
    "encounterDatetime": "2026-10-01T10:00:00.000+0530",
    "encounterProviders": [{"display": "Dr Rao: Clinician"}],
    "obs": [
        {"concept": {"uuid": "c-cc", "display": "Chief Complaint"}, "value": "Cough"},
        {
            "concept": {"uuid": "c-diag", "display": "Visit Diagnoses"},
            "value": None,
            "groupMembers": [
                {
                    "concept": {"display": "Non-Coded Diagnosis"},
                    "value": "Pneumonia",
                    "comment": "ICD-11 CA40.Z",
                },
                {"concept": {"display": "Diagnosis Certainty"}, "value": {"display": "Confirmed"}},
            ],
        },
    ],
    "orders": [{"drugNonCoded": "Amoxicillin 500mg", "orderType": {"display": "Drug Order"}}],
}


@respx.mock
async def test_get_visit_maps_encounters():
    _login()
    enc = _encounters([ENC_ROW])
    detail = await _client().get_visit("v-1")
    reps = [c.request.url.params["v"] for c in enc.calls]
    assert f"custom:(uuid,encounters:({_ENCOUNTER_REP[8:-1]}))" in reps
    assert not respx.get(ENC_LIST).called  # encounter?visit= without patient 500s
    assert detail is not None and detail.visit.uuid == "v-1"
    e = detail.encounters[0]
    assert (e.uuid, e.encounter_type, e.provider) == ("e-1", "Consultation", "Dr Rao")
    assert e.at == datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)
    assert ("Chief Complaint", "Cough") in e.obs
    assert e.drugs == ["Amoxicillin 500mg"]


@respx.mock
async def test_get_visit_returns_diagnosis_comments():
    _login()
    _encounters([ENC_ROW])
    detail = await _client().get_visit("v-1")
    assert detail is not None
    assert detail.encounters[0].diagnoses == [("Pneumonia", "ICD-11 CA40.Z")]


@respx.mock
async def test_get_visit_unknown_returns_none():
    _login()
    respx.get(f"{VISIT}/nope").mock(return_value=httpx.Response(404))
    assert await _client().get_visit("nope") is None


# ─── write_encounter ─────────────────────────────────────────────────────────


@respx.mock
async def test_write_encounter_payload():
    _login()
    lookup = _no_existing()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    res = await _client().write_encounter(_input())
    assert (res.encounter_uuid, res.created) == ("e-9", True)
    assert lookup.calls[0].request.url.params["v"] == (
        f"custom:(uuid,encounters:({_MARKER_REP[8:-1]}))"
    )
    assert json.loads(post.calls[0].request.content) == {
        "patientUuid": "p-1",
        "visitUuid": "v-1",
        "locationUuid": "loc-1",
        "encounterTypeUuid": "et-consult",
        "encounterDateTime": AT_MS,
        "providers": [{"uuid": "pr-1"}],
        "observations": [
            {"concept": {"uuid": "c-cc"}, "value": "Cough"},
            {"concept": {"uuid": "c-pulse"}, "value": 88.0},
            {"concept": {"uuid": "c-marker"}, "value": str(NOTE)},
        ],
        "bahmniDiagnoses": [
            {
                "freeTextAnswer": "Pneumonia",
                "order": "PRIMARY",
                "certainty": "CONFIRMED",
                "comments": "ICD-11 CA40.Z",
            }
        ],
        "drugOrders": [
            {
                "careSetting": "OUTPATIENT",
                "drugNonCoded": "Amoxicillin 500mg",
                "concept": {"uuid": "c-drug-other"},
                "dosingInstructionType": FLEX,
                "dosingInstructions": {
                    "administrationInstructions": json.dumps(
                        {"dose": "1 tab", "frequency": "TDS", "instructions": "after food"}
                    ),
                    # Bahmni NPEs without asNeeded/quantity; quantityUnits must be a concept.
                    "asNeeded": False,
                    "quantity": 1,
                    "quantityUnits": "Tablet",  # the configured name is what is sent
                },
                "duration": 5,
                "durationUnits": "Days",
            }
        ],
    }


@respx.mock
async def test_write_encounter_diagnosis_comment_carries_code():
    _login()
    _no_existing()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    diagnoses = [
        RecordDiagnosis(code="CA40.Z", title="Pneumonia", primary=True),
        RecordDiagnosis(code=None, title="Fatigue", primary=False),
    ]
    await _client().write_encounter(_input(diagnoses=diagnoses, drug_orders=[]))
    sent = json.loads(post.calls[0].request.content)["bahmniDiagnoses"]
    assert sent[0]["freeTextAnswer"] == "Pneumonia" and sent[0]["comments"] == "ICD-11 CA40.Z"
    assert sent[1]["freeTextAnswer"] == "Fatigue" and sent[1]["order"] == "SECONDARY"
    assert "comments" not in sent[1]


@respx.mock
async def test_write_encounter_idempotent_on_marker():
    _login()
    row = {
        "uuid": "e-old",
        "obs": [
            {"concept": {"uuid": "c-cc"}, "value": str(NOTE)},  # wrong concept: ignored
            {"concept": {"uuid": "c-marker"}, "value": str(NOTE)},
        ],
    }
    _encounters([row])
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "x"}))
    res = await _client().write_encounter(_input())
    assert (res.encounter_uuid, res.created) == ("e-old", False)
    assert post.call_count == 0


@respx.mock
async def test_write_encounter_other_note_marker_does_not_match():
    _login()
    row = {"uuid": "e-old", "obs": [{"concept": {"uuid": "c-marker"}, "value": "someone-else"}]}
    _encounters([row])
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    res = await _client().write_encounter(_input())
    assert res.created is True and post.call_count == 1


@respx.mock
async def test_write_encounter_marker_lookup_fails_closed():
    _login()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    respx.get(f"{VISIT}/v-1").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().write_encounter(_input())
    respx.get(f"{VISIT}/v-1").mock(return_value=httpx.Response(200, json={"uuid": "v-1"}))
    with pytest.raises(VendorRejected):
        await _client().write_encounter(_input())
    assert post.call_count == 0


def test_custom_reps_never_continue_after_a_double_close():
    # OpenMRS's custom-rep parser misreads "...)),next": every deep nest must sit last.
    for rep in (_ENCOUNTER_REP, _MARKER_REP):
        nested = f"custom:(uuid,encounters:({rep[8:-1]}))"
        assert "))," not in rep and "))," not in nested


@respx.mock
async def test_write_encounter_missing_concept_raises_before_post():
    _login()
    lookup = _no_existing()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    with pytest.raises(ConfigError, match="concept_map missing vitals_spo2"):
        await _client().write_encounter(_input(obs=[RecordObs("vitals_spo2", 97.0)]))
    assert post.call_count == 0 and lookup.call_count == 0


@respx.mock
async def test_write_encounter_requires_marker_concept_and_types():
    _login()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    with pytest.raises(ConfigError):
        await _client(note_marker_concept_uuid="").write_encounter(_input())
    with pytest.raises(ConfigError):
        await _client(consultation_encounter_type_uuid="").write_encounter(_input())
    with pytest.raises(ConfigError):
        await _client(drug_other_concept_uuid="").write_encounter(_input())
    assert post.call_count == 0


@respx.mock
async def test_write_encounter_uses_given_encounter_type():
    _login()
    _no_existing()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    await _client().write_encounter(_input(encounter_type_uuid="et-summary"))
    assert json.loads(post.calls[0].request.content)["encounterTypeUuid"] == "et-summary"
    # A given type needs no consultation type configured.
    await _client(consultation_encounter_type_uuid="").write_encounter(
        _input(encounter_type_uuid="et-summary")
    )
    assert json.loads(post.calls[1].request.content)["encounterTypeUuid"] == "et-summary"
    # None (the default) keeps the consultation type.
    await _client().write_encounter(_input(encounter_type_uuid=None))
    assert json.loads(post.calls[2].request.content)["encounterTypeUuid"] == "et-consult"


@respx.mock
async def test_write_encounter_empty_given_type_config_error_before_http():
    # An unset discharge_summary_encounter_type_uuid must not file a consultation.
    _login()
    lookup = _no_existing()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    with pytest.raises(ConfigError, match="encounter_type_uuid"):
        await _client().write_encounter(_input(encounter_type_uuid=""))
    assert post.call_count == 0 and lookup.call_count == 0


@pytest.mark.parametrize(
    "key",
    [
        "admission_encounter_type_uuid",
        "transfer_encounter_type_uuid",
        "discharge_encounter_type_uuid",
    ],
)
@respx.mock
async def test_write_encounter_refuses_adt_encounter_type(key: str):
    # ADT types go through admit/transfer/discharge (bed, visit status), never bahmniencounter.
    _login()
    lookup = _no_existing()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    with pytest.raises(ConfigError, match=key):
        await _client(**{key: "et-adt"}).write_encounter(_input(encounter_type_uuid="et-adt"))
    assert post.call_count == 0 and lookup.call_count == 0


@respx.mock
async def test_drug_orders_without_quantity_units_raise_before_any_call():
    _login()
    lookup = _no_existing()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    with pytest.raises(ConfigError, match="drug_quantity_units"):
        await _client(drug_quantity_units="").write_encounter(_input())
    assert post.call_count == 0 and lookup.call_count == 0


@respx.mock
async def test_no_drug_orders_needs_no_quantity_units():
    _login()
    _no_existing()
    post = respx.post(WRITE).mock(return_value=httpx.Response(200, json={"encounterUuid": "e-9"}))
    res = await _client(drug_quantity_units="").write_encounter(_input(drug_orders=[]))
    assert res.created and post.call_count == 1


@respx.mock
async def test_start_visit_at_visit_location_when_configured():
    # Bahmni keeps visits at the Visit Location (its own visits are created there), and
    # its stock ADT worklist SQL lists a visit only when visit.location_id IS the visit
    # location: a visit at the login location never shows on "to admit".
    _login()
    respx.get(VISIT).mock(return_value=httpx.Response(200, json={"results": []}))
    post = respx.post(VISIT).mock(return_value=httpx.Response(201, json=_visit("v-2")))
    await _client(visit_location_uuid="vloc-1").start_visit("p-1")
    assert json.loads(post.calls[0].request.content)["location"] == "vloc-1"
