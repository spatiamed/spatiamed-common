"""Lab test catalogue and lab orders on the consultation encounter (synthetic data)."""

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from sm_common.integrations.adapters import bahmni_orders
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import ConfigError, NotFound, VendorRejected
from sm_common.integrations.record_types import (
    RecordDiagnosis,
    RecordDrugOrder,
    RecordEncounterInput,
    RecordLabOrder,
    RecordLabTest,
    RecordObs,
)

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
WRITE = f"{REST}/bahmnicore/bahmniencounter"
NOTE = UUID("0b1c2d3e-0000-4000-8000-00000000beef")
AT = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)
AT_MS = 1790829000000

VISIT = "fea1e225-6520-4924-90e4-8be34c10eef6"
ORDERABLE_SET = "500d0d3d-5eb3-430c-acc5-114580084f39"
LAB_TYPE = "8c53c38d-620e-4235-abee-082748a7b118"
DRUG_TYPE = "bab0a675-5109-40c5-9fb3-80b40a51341b"
OTHER_TYPE = "6bf75d50-b5ef-4622-8ea0-2d96f0793ea3"
TEST_HB = "6dac5a59-e44a-4e9b-ba9d-b06e74680aa9"
TEST_GLUCOSE = "69bc864f-65c7-46a5-82fb-1ff625268b39"
PANEL_LFT = "bebabd47-d514-4a7a-81fe-769d6f47b96e"
TEST_ALT = "3631d761-b04b-45a4-854c-917b5a6c3cdb"
SAMPLE_URINE = "69676157-9357-4db3-b624-2f1a8c6a08c7"
TEST_URINE_RE = "f4ebb9b7-f90f-4ded-8edb-4b2ed342c7f9"
MISC_CONCEPT = "df8b1d80-9b68-49a4-b5bf-6d74d8862f15"
ACTIVE_ORDER = "a36155ce-ba51-4726-b8eb-b1dbeb29f4cf"
STOPPED_ORDER = "ed5e8711-5639-44b4-89f9-1efda4b5d0f9"
ENC = "a33fa14d-5bcb-420a-8180-49395c630323"
DRUG_ONLY = frozenset(
    {
        "drug",
        "drugNonCoded",
        "dose",
        "doseUnits",
        "frequency",
        "duration",
        "durationUnits",
        "quantity",
        "quantityUnits",
        "dosingInstructions",
    }
)


def _client(**over: object) -> BahmniRecordClient:
    base: dict[str, object] = {
        "location_uuid": "loc-1",
        "identifier_type_uuid": "idtype",
        "identifier_source_uuid": "idsrc",
        "consultation_encounter_type_uuid": "et-consult",
        "note_marker_concept_uuid": "c-marker",
        "drug_other_concept_uuid": "c-drug-other",
        "drug_quantity_units": "Tablet",
        "concept_map": {"chief_complaint": "c-cc"},
        "lab_order_type_uuid": LAB_TYPE,
        "lab_orderable_set_uuid": ORDERABLE_SET,
    }
    base.update(over)
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig(**base))  # type: ignore[arg-type]


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _member(uuid: str, name: str, cls: str, members: list[Any] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "uuid": uuid,
        "display": name,
        "retired": False,
        "conceptClass": {"name": cls},
    }
    if members is not None:
        row["setMembers"] = members
    return row


def _orderable_set() -> dict[str, Any]:
    return {
        "uuid": ORDERABLE_SET,
        "setMembers": [
            _member(TEST_HB, "LT-P3 Haemoglobin", "Test", []),
            _member(
                PANEL_LFT,
                "LT-P3 Liver Panel",
                "LabSet",
                [
                    _member(TEST_ALT, "LT-P3 ALT", "Test"),
                    # Also a direct member: listed once.
                    _member(TEST_HB, "LT-P3 Haemoglobin", "Test"),
                ],
            ),
            _member(
                SAMPLE_URINE,
                "LT-P3 Urine",
                "Sample",
                [_member(TEST_URINE_RE, "LT-P3 Urine Routine", "Test")],
            ),
            _member(MISC_CONCEPT, "LT-P3 Misc", "Misc", []),
            {**_member(TEST_GLUCOSE, "LT-P3 Glucose", "Test", []), "retired": True},
        ],
    }


def _set_route(body: Any = None, status: int = 200) -> respx.Route:
    return respx.get(f"{REST}/concept/{ORDERABLE_SET}").mock(
        return_value=httpx.Response(status, json=_orderable_set() if body is None else body)
    )


# ─── catalogue ───────────────────────────────────────────────────────────────


@respx.mock
async def test_lab_tests_from_orderable_set() -> None:
    _login()
    route = _set_route()
    tests = await _client().list_lab_tests()
    assert tests == [
        RecordLabTest(
            concept_uuid=TEST_HB, name="LT-P3 Haemoglobin", is_panel=False, sample_type=None
        ),
        RecordLabTest(
            concept_uuid=PANEL_LFT, name="LT-P3 Liver Panel", is_panel=True, sample_type=None
        ),
        RecordLabTest(concept_uuid=TEST_ALT, name="LT-P3 ALT", is_panel=False, sample_type=None),
        RecordLabTest(
            concept_uuid=TEST_URINE_RE,
            name="LT-P3 Urine Routine",
            is_panel=False,
            sample_type="LT-P3 Urine",
        ),
    ]
    rep = route.calls[0].request.url.params["v"]
    # The recursive member group is last at every level (OpenMRS rep parser).
    assert rep.endswith("conceptClass:(name)))))")
    assert rep.count("setMembers:(") == 3
    for group in rep.split("setMembers:(")[1:-1]:
        assert group.endswith(",")


@respx.mock
async def test_lab_tests_cached_per_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _login()
    route = _set_route()
    now = [1000.0]
    monkeypatch.setattr(bahmni_orders, "_monotonic", lambda: now[0])
    client = _client()
    first = await client.list_lab_tests()
    now[0] += 599
    assert await client.list_lab_tests() == first
    assert route.call_count == 1
    now[0] += 2
    await client.list_lab_tests()
    assert route.call_count == 2
    # Another instance does not share the cache.
    await _client().list_lab_tests()
    assert route.call_count == 3


@respx.mock
async def test_lab_tests_failure_not_cached() -> None:
    _login()
    route = _set_route(body=[])
    client = _client()
    with pytest.raises(VendorRejected):
        await client.list_lab_tests()
    route.mock(return_value=httpx.Response(200, json=_orderable_set()))
    assert len(await client.list_lab_tests()) == 4


@respx.mock
async def test_lab_tests_requires_set_key() -> None:
    route = respx.route()
    with pytest.raises(ConfigError):
        await _client(lab_orderable_set_uuid="").list_lab_tests()
    with pytest.raises(ConfigError):
        await _client(lab_orderable_set_uuid="x/../patient").list_lab_tests()
    assert route.call_count == 0


@respx.mock
async def test_lab_tests_missing_set_and_bad_body() -> None:
    _login()
    _set_route(body={"error": "x"}, status=404)
    with pytest.raises(NotFound):
        await _client().list_lab_tests()
    respx.reset()
    _login()
    for bad in ([], {"uuid": ORDERABLE_SET}, {"setMembers": "x"}):
        _set_route(body=bad)
        with pytest.raises(VendorRejected):
            await _client().list_lab_tests()


# ─── lab orders on write_encounter ───────────────────────────────────────────


def _input(**over: object) -> RecordEncounterInput:
    base: dict[str, object] = {
        "note_id": NOTE,
        "patient_uuid": "p-1",
        "visit_uuid": VISIT,
        "provider_uuid": "pr-1",
        "encounter_at": AT,
        "obs": [RecordObs("chief_complaint", "Cough")],
        "diagnoses": [RecordDiagnosis(code="CA40.Z", title="Test Diagnosis", primary=True)],
        "drug_orders": [
            RecordDrugOrder(
                name="LT-P3 Test Tablet",
                dose="1",
                frequency="BD",
                duration_days=5,
                instructions="after food",
            )
        ],
    }
    base.update(over)
    return RecordEncounterInput(**base)  # type: ignore[arg-type]


def _lab_order_row(
    uuid: str,
    concept: str,
    *,
    type_uuid: str = LAB_TYPE,
    type_name: str = "Lab Order",
    care: str = "OUTPATIENT",
    stopped: str | None = None,
    expires: str | None = None,
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "orderType": {"uuid": type_uuid, "name": type_name},
        "concept": {"uuid": concept, "display": "LT-P3 test"},
        "careSetting": {"careSettingType": care},
        "dateActivated": "2026-10-01T10:00:00.000+0530",
        "dateStopped": stopped,
        "autoExpireDate": expires,
        "action": "NEW",
    }


def _visit(order_rows: list[Any], marker_rows: list[Any] | None = None) -> respx.Route:
    """GET visit/<uuid>: the orders rep answers ``order_rows``, the marker rep the obs."""

    def answer(req: httpx.Request) -> httpx.Response:
        rep = req.url.params.get("v", "")
        if set(re.findall(r"[A-Za-z]+", rep)) & DRUG_ONLY:
            # As Bahmni: a DrugOrder-only field fails the read once a lab order exists.
            return httpx.Response(500, json={"error": {"message": "Unknown property"}})
        if "orders:(" in rep:
            rows = [{"uuid": ENC, "orders": order_rows}]
        elif "obs:(" in rep:
            rows = marker_rows or []
        else:  # pragma: no cover - a rep the client should not send
            return httpx.Response(500)
        return httpx.Response(200, json={"uuid": VISIT, "encounters": rows})

    return respx.get(f"{REST}/visit/{VISIT}").mock(side_effect=answer)


def _write_route() -> respx.Route:
    return respx.post(WRITE).mock(
        return_value=httpx.Response(200, json={"encounterUuid": str(NOTE)})
    )


def _posted(route: respx.Route) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls[0].request.content)
    return body


@respx.mock
async def test_write_encounter_adds_lab_orders() -> None:
    _login()
    visit = _visit([])
    write = _write_route()
    res = await _client().write_encounter(
        _input(
            lab_orders=[
                RecordLabOrder(concept_uuid=TEST_HB),
                RecordLabOrder(concept_uuid=PANEL_LFT, urgency="STAT", comment="LT-P3 fasting"),
                # A repeat inside one note is sent once (Bahmni refuses duplicates).
                RecordLabOrder(concept_uuid=TEST_HB, urgency="STAT"),
            ]
        )
    )
    assert res.created is True
    body = _posted(write)
    assert body["orders"] == [
        {
            "concept": {"uuid": TEST_HB},
            "orderTypeUuid": LAB_TYPE,
            "action": "NEW",
            "careSetting": "OUTPATIENT",
            "urgency": "ROUTINE",
        },
        {
            "concept": {"uuid": PANEL_LFT},
            "orderTypeUuid": LAB_TYPE,
            "action": "NEW",
            "careSetting": "OUTPATIENT",
            "urgency": "STAT",
            "commentToFulfiller": "LT-P3 fasting",
        },
    ]
    # Drug orders are unaffected.
    assert body["drugOrders"][0]["drugNonCoded"] == "LT-P3 Test Tablet"
    reps = [c.request.url.params["v"] for c in visit.calls]
    assert len(reps) == 2 and any("orders:(" in r for r in reps)


@respx.mock
async def test_already_active_lab_concept_not_reposted() -> None:
    _login()
    _visit(
        [
            _lab_order_row(ACTIVE_ORDER, TEST_HB),
            # Stopped: no longer active, so re-ordering it is allowed.
            _lab_order_row(STOPPED_ORDER, TEST_ALT, stopped="2026-10-01T11:00:00.000+0530"),
        ]
    )
    write = _write_route()
    res = await _client().write_encounter(
        _input(
            lab_orders=[
                RecordLabOrder(concept_uuid=TEST_HB),
                RecordLabOrder(concept_uuid=TEST_ALT),
                # A repeat inside the note is not an "already active" skip.
                RecordLabOrder(concept_uuid=TEST_ALT),
            ]
        )
    )
    assert [o["concept"]["uuid"] for o in _posted(write)["orders"]] == [TEST_ALT]
    # The caller is told which ordered concepts were dropped as already active.
    assert res.created is True
    assert res.skipped_lab_concepts == (TEST_HB,)


# Bahmni stamps every encounter order with an expiry (encounter session length).
NOW = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


@respx.mock
async def test_expired_lab_order_is_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    _login()
    monkeypatch.setattr(bahmni_orders, "_utcnow", lambda: NOW)
    visit = _visit(
        [
            # Day 1 order, expired an hour after it was placed: Bahmni accepts a repeat.
            _lab_order_row(ACTIVE_ORDER, TEST_HB, expires="2026-10-01T11:00:00.000+0530"),
            # Expires exactly now: still active.
            _lab_order_row(STOPPED_ORDER, TEST_ALT, expires="2026-10-03T06:00:00.000Z"),
        ]
    )
    write = _write_route()
    res = await _client().write_encounter(
        _input(
            lab_orders=[RecordLabOrder(concept_uuid=TEST_HB), RecordLabOrder(concept_uuid=TEST_ALT)]
        )
    )
    assert [o["concept"]["uuid"] for o in _posted(write)["orders"]] == [TEST_HB]
    assert res.skipped_lab_concepts == (TEST_ALT,)
    reps = [c.request.url.params["v"] for c in visit.calls]
    assert any("autoExpireDate" in r for r in reps if "orders:(" in r)


@respx.mock
async def test_unexpired_lab_order_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _login()
    monkeypatch.setattr(bahmni_orders, "_utcnow", lambda: NOW)
    _visit([_lab_order_row(ACTIVE_ORDER, TEST_HB, expires="2026-10-03T12:00:00.000+0530")])
    write = _write_route()
    res = await _client().write_encounter(_input(lab_orders=[RecordLabOrder(concept_uuid=TEST_HB)]))
    assert "orders" not in _posted(write)
    assert res.skipped_lab_concepts == (TEST_HB,)


@respx.mock
async def test_expired_order_still_listed_as_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """The expiry is only for the duplicate check: pending reads keep expired orders."""
    _login()
    monkeypatch.setattr(bahmni_orders, "_utcnow", lambda: NOW)
    _visit([_lab_order_row(ACTIVE_ORDER, TEST_HB, expires="2026-10-01T11:00:00.000+0530")])
    orders = await _client().list_visit_orders(VISIT)
    assert [o.order_uuid for o in orders] == [ACTIVE_ORDER]
    assert orders[0].auto_expire_date == datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc)


@respx.mock
async def test_non_lab_or_inpatient_order_does_not_suppress_lab_order() -> None:
    _login()
    _visit(
        [
            _lab_order_row(ACTIVE_ORDER, TEST_HB, type_uuid=DRUG_TYPE, type_name="Drug Order"),
            _lab_order_row(STOPPED_ORDER, TEST_HB, type_uuid=OTHER_TYPE, type_name="Radiology"),
            # The duplicate rule is per care setting; our lab orders are OUTPATIENT.
            _lab_order_row(ENC, TEST_ALT, care="INPATIENT"),
        ]
    )
    write = _write_route()
    per_order = respx.get(url__regex=rf"{re.escape(REST)}/order/.+").mock(
        return_value=httpx.Response(500)
    )
    res = await _client(drug_order_type_uuid=DRUG_TYPE).write_encounter(
        _input(
            lab_orders=[RecordLabOrder(concept_uuid=TEST_HB), RecordLabOrder(concept_uuid=TEST_ALT)]
        )
    )
    assert [o["concept"]["uuid"] for o in _posted(write)["orders"]] == [TEST_HB, TEST_ALT]
    assert res.skipped_lab_concepts == ()
    # The duplicate check reads lab orders only: no per-drug-order read.
    assert per_order.call_count == 0


@respx.mock
async def test_all_lab_orders_active_omits_orders_key() -> None:
    _login()
    _visit([_lab_order_row(ACTIVE_ORDER, TEST_HB)])
    write = _write_route()
    await _client().write_encounter(_input(lab_orders=[RecordLabOrder(concept_uuid=TEST_HB)]))
    assert "orders" not in _posted(write)


@respx.mock
async def test_existing_note_returns_before_lab_read() -> None:
    _login()
    marker = [{"uuid": "enc-old", "obs": [{"concept": {"uuid": "c-marker"}, "value": str(NOTE)}]}]
    visit = _visit([], marker_rows=marker)
    write = _write_route()
    res = await _client().write_encounter(_input(lab_orders=[RecordLabOrder(concept_uuid=TEST_HB)]))
    assert (res.encounter_uuid, res.created) == ("enc-old", False)
    assert write.call_count == 0
    assert visit.call_count == 1


@respx.mock
async def test_encounter_without_lab_orders_unchanged() -> None:
    _login()
    visit = _visit([])
    write = _write_route()
    await _client().write_encounter(_input())
    raw = write.calls[0].request.content
    expected = {
        "encounterUuid": str(NOTE),
        "patientUuid": "p-1",
        "visitUuid": VISIT,
        "locationUuid": "loc-1",
        "encounterTypeUuid": "et-consult",
        "encounterDateTime": AT_MS,
        "providers": [{"uuid": "pr-1"}],
        "observations": [
            {"concept": {"uuid": "c-cc"}, "value": "Cough"},
            {"concept": {"uuid": "c-marker"}, "value": str(NOTE)},
        ],
        "bahmniDiagnoses": [
            {
                "freeTextAnswer": "Test Diagnosis",
                "order": "PRIMARY",
                "certainty": "CONFIRMED",
                "comments": "ICD-11 CA40.Z",
            }
        ],
        "drugOrders": [
            {
                "careSetting": "OUTPATIENT",
                "drugNonCoded": "LT-P3 Test Tablet",
                "concept": {"uuid": "c-drug-other"},
                "dosingInstructionType": (
                    "org.openmrs.module.bahmniemrapi.drugorder.dosinginstructions."
                    "FlexibleDosingInstructions"
                ),
                "dosingInstructions": {
                    "administrationInstructions": json.dumps(
                        {"dose": "1", "frequency": "BD", "instructions": "after food"}
                    ),
                    "asNeeded": False,
                    "quantity": 1,
                    "quantityUnits": "Tablet",
                },
                "duration": 5,
                "durationUnits": "Days",
            }
        ],
    }
    # Byte-equal to the body httpx serialises today (key order included).
    assert raw == httpx.Request("POST", WRITE, json=expected).content
    # No lab read: only the marker lookup.
    assert visit.call_count == 1
    assert "orders:(" not in visit.calls[0].request.url.params["v"]


@respx.mock
async def test_lab_orders_require_type_key() -> None:
    route = respx.route()
    with pytest.raises(ConfigError):
        await _client(lab_order_type_uuid="").write_encounter(
            _input(lab_orders=[RecordLabOrder(concept_uuid=TEST_HB)])
        )
    assert route.call_count == 0


@respx.mock
@pytest.mark.parametrize(
    "order",
    [
        RecordLabOrder(concept_uuid=""),
        RecordLabOrder(concept_uuid="x/../y"),
        RecordLabOrder(concept_uuid=TEST_HB, urgency="ON_SCHEDULED_DATE"),  # type: ignore[arg-type]
        RecordLabOrder(concept_uuid=TEST_HB, comment="x" * 1025),
    ],
)
async def test_bad_lab_order_rejected_before_io(order: RecordLabOrder) -> None:
    route = respx.route()
    with pytest.raises(ValueError):
        await _client().write_encounter(_input(lab_orders=[order]))
    assert route.call_count == 0


@respx.mock
async def test_non_string_lab_comment_named_as_type_error() -> None:
    route = respx.route()
    bad = RecordLabOrder(concept_uuid=TEST_HB, comment=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a string"):
        await _client().write_encounter(_input(lab_orders=[bad]))
    assert route.call_count == 0


@respx.mock
async def test_lab_tests_accept_labtest_class() -> None:
    """Some distributions class lab tests as ``LabTest`` rather than ``Test``."""
    _login()
    body = {
        "uuid": ORDERABLE_SET,
        "setMembers": [
            _member(TEST_HB, "LT-P3 Haemoglobin", "LabTest", []),
            _member(
                PANEL_LFT,
                "LT-P3 Liver Panel",
                "LabSet",
                [_member(TEST_ALT, "LT-P3 ALT", "LabTest")],
            ),
        ],
    }
    _set_route(body=body)
    tests = await _client().list_lab_tests()
    assert [(t.concept_uuid, t.is_panel) for t in tests] == [
        (TEST_HB, False),
        (PANEL_LFT, True),
        (TEST_ALT, False),
    ]
