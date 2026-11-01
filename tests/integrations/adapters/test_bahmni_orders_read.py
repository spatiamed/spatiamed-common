import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from sm_common.integrations.adapters import bahmni_orders
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import NotFound, VendorRejected

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
FIX = Path(__file__).parent.parent / "fixtures" / "bahmni"

VISIT = "c68e39a6-ebc2-481b-8ea4-f9532b9a8068"
ENC_1 = "7150aa91-5f1c-4e25-acd0-e27dd5c87b77"
ENC_2 = "7a3bff1c-baf7-462c-9892-b5557fd62922"
NONCODED = "04359d13-1f52-4aa1-9e71-d59000efe55e"
LAB = "948fb6c3-10aa-4fec-b46f-76d194a6ad9d"
RADIOLOGY = "58ba8aad-fddd-4fb3-a6c7-d2ca72e60b3f"
CODED_STOPPED = "d61c5fe3-6afb-40ab-b893-eb76a8d2b0ac"
REVISED = "a31c2412-7c6c-4a5e-bf5d-389a94c97d7d"
NONCODED_STOPPED = "bc02f2bc-5848-477a-bc1e-d4580d50ec58"
DISCONTINUE = "8bd55fd4-6b18-4c45-8cb6-9a31c0d50e0f"
PATIENT = "0ae94ce1-c968-4856-9254-74995c495b2e"
OPD_ORDER = "6d21a47f-8185-4bf1-a139-8cd40c88d42b"
IPD_ORDER = "f04118b9-d00c-4462-9692-3f6e0786e20f"
IPD_ENC = "41b0dfea-1866-4907-abc1-8543d7818a54"
IPD_VISIT = "4180bc00-b700-423f-a560-82ffe7763c24"
LAB_TYPE = "cf962676-1c5f-4d92-a9b0-6aeb380661e4"
RADIOLOGY_TYPE = "7fa29911-d0ac-4666-a9cb-c234661c125c"


def _fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def _client(**over: object) -> BahmniRecordClient:
    base: dict[str, object] = {
        "location_uuid": "loc-1",
        "identifier_type_uuid": "idtype",
        "identifier_source_uuid": "idsrc",
    }
    base.update(over)
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig(**base))  # type: ignore[arg-type]


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


# Fields only org.openmrs.DrugOrder has. Bahmni's lab orders are plain Orders, so naming
# any of these in a rep that also reads a lab order fails the whole read.
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


def _rep_names(rep: str) -> set[str]:
    return set(re.findall(r"[A-Za-z]+", rep))


def _visit_route(body: Any) -> respx.Route:
    """A Bahmni-like server: the visit read answers generic Order fields only, and
    refuses (as Bahmni does) a rep naming a DrugOrder-only field; each drug order's
    own fields come from ``GET order/<uuid>``."""
    orders = {o["uuid"]: o for enc in body.get("encounters", []) for o in enc.get("orders", [])}

    def visit(req: httpx.Request) -> httpx.Response:
        if _rep_names(req.url.params.get("v", "")) & DRUG_ONLY:
            return httpx.Response(500, json={"error": {"message": "Unknown property"}})
        generic = copy.deepcopy(body)
        for enc in generic.get("encounters", []):
            enc["orders"] = [
                {k: v for k, v in o.items() if k not in DRUG_ONLY} for o in enc.get("orders", [])
            ]
        return httpx.Response(200, json=generic)

    def order(req: httpx.Request, uuid: str) -> httpx.Response:
        o = orders.get(uuid)
        if o is None:
            return httpx.Response(404, json={})
        names = _rep_names(req.url.params.get("v", ""))
        if names & DRUG_ONLY and "drugNonCoded" not in o:  # not a DrugOrder
            return httpx.Response(500, json={"error": {"message": "Unknown property"}})
        return httpx.Response(200, json={k: v for k, v in o.items() if k in names})

    respx.get(url__regex=rf"{re.escape(REST)}/order/(?P<uuid>[^/?]+)").mock(side_effect=order)
    return respx.get(f"{REST}/visit/{VISIT}").mock(side_effect=visit)


def _order_reads() -> list[httpx.Request]:
    return [c.request for c in respx.calls if "/order/" in c.request.url.path]


@respx.mock
async def test_noncoded_drug_order_parsed() -> None:
    _login()
    _visit_route(_fixture("visit_orders.json"))
    orders = await _client().list_visit_orders(VISIT)
    o = next(o for o in orders if o.order_uuid == NONCODED)
    assert o.kind == "drug"
    assert o.display_name == "LT-P3 Test Syrup 5ml"
    # Non-coded: the "other drug" concept is not the drug's identity.
    assert o.concept_uuid is None
    assert o.drug_uuid is None
    assert o.encounter_uuid == ENC_1
    assert o.visit_uuid == VISIT
    assert o.care_setting == "OUTPATIENT"
    # Structured dose/frequency are null on our pushes; recovered from the JSON.
    assert o.dosing_source == "administration_json"
    assert o.dose == 2.0
    assert o.dose_text == "2"
    assert o.dose_units is None
    assert o.frequency == "Twice a day"
    assert o.instructions == "After food"
    # Reported as-is: sm_common cannot know 1 is a placeholder.
    assert o.quantity == 1.0
    assert o.quantity_units == "Tablet"
    assert o.duration == 5
    assert o.duration_units == "Days"
    assert o.orderer_provider_uuid == "adf70be4-5780-4bd6-91c7-37159e50baf2"
    assert o.orderer_name == "Test Doctor"
    assert o.date_activated == datetime(2026, 10, 1, 4, 45, tzinfo=timezone.utc)
    assert o.date_stopped is None
    assert o.action == "NEW"
    assert o.previous_order_uuid is None


@respx.mock
async def test_unparseable_administration_json_leaves_fields_null() -> None:
    _login()
    body = _fixture("visit_orders.json")
    order = body["encounters"][0]["orders"][0]
    order["dosingInstructions"] = '{"dose": "2", "frequency": '  # truncated JSON
    _visit_route(body)
    o = next(o for o in await _client().list_visit_orders(VISIT) if o.order_uuid == NONCODED)
    assert o.dosing_source == "none"
    assert (o.dose, o.dose_text, o.frequency, o.instructions) == (None, None, None, None)

    # A JSON value that is not an object, or a free-text dose, is never guessed at.
    respx.reset()
    _login()
    for raw, dose, dose_text in (
        ('["2", "BD"]', None, None),
        ('{"dose": "1 tab", "frequency": "BD", "instructions": ""}', None, "1 tab"),
    ):
        body = _fixture("visit_orders.json")
        body["encounters"][0]["orders"][0]["dosingInstructions"] = raw
        _visit_route(body)
        o = next(o for o in await _client().list_visit_orders(VISIT) if o.order_uuid == NONCODED)
        assert (o.dose, o.dose_text) == (dose, dose_text)
        assert o.instructions is None


@respx.mock
async def test_coded_lab_order_parsed_with_concept() -> None:
    _login()
    body = _fixture("visit_orders.json")
    # The lab feed sets the accession to the encounter uuid (set here, not in the
    # fixture, so the secret scanner does not read it as a key).
    body["encounters"][0]["orders"][1]["accessionNumber"] = ENC_1
    _visit_route(body)
    orders = await _client().list_visit_orders(VISIT)
    o = next(o for o in orders if o.order_uuid == LAB)
    assert o.kind == "lab"
    assert o.display_name == "Test Haemoglobin"
    assert o.concept_uuid == "a9539872-5045-423f-a2d0-0d45fc62ebbf"
    assert o.drug_uuid is None
    assert o.accession_number == ENC_1
    assert o.fulfiller_status == "RECEIVED"
    assert o.dosing_source == "none"
    # Radiology is "other" and not in the default kinds.
    assert RADIOLOGY not in {x.order_uuid for x in orders}
    others = await _client().list_visit_orders(VISIT, kinds=frozenset({"other"}))
    assert [x.order_uuid for x in others] == [RADIOLOGY]
    assert others[0].kind == "other"


@respx.mock
async def test_coded_drug_order_uses_structured_dosing() -> None:
    _login()
    _visit_route(_fixture("visit_orders.json"))
    orders = await _client().list_visit_orders(VISIT)
    o = next(o for o in orders if o.order_uuid == REVISED)
    assert o.display_name == "Test Paracetamol 500mg Tablet"
    assert o.drug_uuid == "3a753c50-ee6e-4eeb-a00f-883fa5c6a616"
    assert o.concept_uuid == "59b09fcf-9b15-41a5-b6ec-d5135aa49caf"
    assert o.dosing_source == "structured"
    assert (o.dose, o.dose_units, o.frequency) == (2.0, "Tablet", "Twice a day")
    assert o.instructions == "After meals"
    assert o.action == "REVISE"
    assert o.previous_order_uuid == CODED_STOPPED
    assert o.encounter_uuid == ENC_2


@respx.mock
async def test_discontinued_excluded_when_active_only() -> None:
    _login()
    _visit_route(_fixture("visit_orders.json"))
    active = {o.order_uuid for o in await _client().list_visit_orders(VISIT)}
    assert active == {NONCODED, LAB, REVISED}
    every = await _client().list_visit_orders(VISIT, active_only=False)
    by_uuid = {o.order_uuid: o for o in every}
    assert set(by_uuid) == {NONCODED, LAB, REVISED, CODED_STOPPED, NONCODED_STOPPED, DISCONTINUE}
    assert by_uuid[CODED_STOPPED].date_stopped == datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc)
    assert by_uuid[DISCONTINUE].action == "DISCONTINUE"
    assert by_uuid[DISCONTINUE].previous_order_uuid == NONCODED_STOPPED


@respx.mock
async def test_voided_order_never_returned() -> None:
    _login()
    body = _fixture("visit_orders.json")
    body["encounters"][0]["orders"][0]["voided"] = True
    _visit_route(body)
    every = await _client().list_visit_orders(VISIT, active_only=False)
    assert NONCODED not in {o.order_uuid for o in every}


@respx.mock
async def test_rep_nests_last() -> None:
    _login()
    route = _visit_route(_fixture("visit_orders.json"))
    await _client().list_visit_orders(VISIT)
    rep = route.calls.last.request.url.params["v"]
    # OpenMRS's custom-rep parser misreads "...)),next": every deep nest must sit last.
    assert "))," not in rep
    assert rep.startswith("custom:(uuid,encounters:(uuid,orders:(")
    for field in (
        "orderType:(uuid,name)",
        "concept:(uuid,display)",
        "careSetting:(careSettingType)",
        "orderer:(uuid,display)",
        "previousOrder:(uuid)",
        "accessionNumber",
        "fulfillerStatus",
        "autoExpireDate",
    ):
        assert field in rep
    # The generic visit rep never names a DrugOrder-only field.
    assert not _rep_names(rep) & DRUG_ONLY
    # The per-drug-order read carries them, nested once and with nothing after "))".
    drug_reps = {r.url.params["v"] for r in _order_reads()}
    assert len(drug_reps) == 1
    (drug_rep,) = drug_reps
    assert "))," not in drug_rep
    assert _rep_names(drug_rep) >= DRUG_ONLY

    respx.reset()
    _login()
    search = respx.get(f"{REST}/order").mock(
        return_value=httpx.Response(200, json=_fixture("drug_orders_active.json"))
    )
    await _client().list_active_orders(PATIENT, care_setting="OUTPATIENT", kind="drug")
    rep = search.calls.last.request.url.params["v"]
    assert "))," not in rep
    assert rep.endswith("encounter:(uuid,visit:(uuid)))")
    # t=drugorder answers DrugOrders only: the drug fields ride in the search rep.
    assert _rep_names(rep) >= DRUG_ONLY

    # t=testorder answers no DrugOrder: its rep names no DrugOrder-only field.
    search.calls.reset()
    search.mock(return_value=httpx.Response(200, json={"results": []}))
    await _client().list_active_orders(PATIENT, care_setting="OUTPATIENT", kind="lab")
    rep = search.calls.last.request.url.params["v"]
    assert "))," not in rep
    assert rep.endswith("encounter:(uuid,visit:(uuid)))")
    assert not _rep_names(rep) & DRUG_ONLY


def test_generic_order_reps_name_no_drug_only_field() -> None:
    assert not _rep_names(bahmni_orders._VISIT_ORDERS_REP) & DRUG_ONLY
    assert not _rep_names(bahmni_orders._ORDER_FIELDS) & DRUG_ONLY
    assert not _rep_names(bahmni_orders._active_orders_rep("lab")) & DRUG_ONLY
    assert _rep_names(bahmni_orders._active_orders_rep("drug")) >= DRUG_ONLY
    assert _rep_names(bahmni_orders._DRUG_ORDER_DETAIL_REP) >= DRUG_ONLY
    assert bahmni_orders._DRUG_ORDER_KEYS == DRUG_ONLY


@respx.mock
async def test_drug_fields_read_per_active_drug_order_only() -> None:
    _login()
    _visit_route(_fixture("visit_orders.json"))
    await _client().list_visit_orders(VISIT)
    # Active drug orders only (NONCODED, REVISED); never a lab, radiology, stopped or
    # DISCONTINUE order.
    read = sorted(r.url.path.rsplit("/", 1)[-1] for r in _order_reads())
    assert read == sorted([NONCODED, REVISED])


@respx.mock
async def test_lab_only_read_makes_no_per_order_reads() -> None:
    """The lab duplicate check (kinds={"lab"}) costs one visit read, whatever drugs exist."""
    _login()
    visit = _visit_route(_fixture("visit_orders.json"))
    orders = await _client().list_visit_orders(VISIT, kinds=frozenset({"lab"}))
    assert [o.order_uuid for o in orders] == [LAB]
    assert visit.call_count == 1
    assert _order_reads() == []


@respx.mock
async def test_drug_order_gone_before_its_read_keeps_generic_fields() -> None:
    _login()
    # Purged between the visit read and its own read: the order stays, drug fields null.
    respx.get(f"{REST}/order/{NONCODED}").mock(return_value=httpx.Response(404, json={}))
    _visit_route(_fixture("visit_orders.json"))
    o = next(o for o in await _client().list_visit_orders(VISIT) if o.order_uuid == NONCODED)
    assert o.kind == "drug"
    assert o.dosing_source == "none"
    assert (o.dose, o.frequency, o.quantity) == (None, None, None)
    assert o.display_name == "Test Other Drug"


@respx.mock
async def test_drug_order_read_answering_another_uuid_is_rejected() -> None:
    _login()
    respx.get(f"{REST}/order/{NONCODED}").mock(
        return_value=httpx.Response(200, json={"uuid": REVISED, "drugNonCoded": "x"})
    )
    _visit_route(_fixture("visit_orders.json"))
    with pytest.raises(VendorRejected):
        await _client().list_visit_orders(VISIT)


@respx.mock
async def test_inpatient_care_setting_filter() -> None:
    _login()
    route = respx.get(f"{REST}/order").mock(
        return_value=httpx.Response(200, json=_fixture("drug_orders_active.json"))
    )
    orders = await _client().list_active_orders(PATIENT, care_setting="INPATIENT", kind="drug")
    # The server filter is not trusted alone: an OUTPATIENT row in the reply is dropped.
    assert [o.order_uuid for o in orders] == [IPD_ORDER]
    o = orders[0]
    assert o.care_setting == "INPATIENT"
    assert o.encounter_uuid == IPD_ENC
    assert o.visit_uuid == IPD_VISIT
    params = route.calls.last.request.url.params
    assert params["patient"] == PATIENT
    assert params["careSetting"] == "INPATIENT"
    assert params["t"] == "drugorder"

    opd = await _client().list_active_orders(PATIENT, care_setting="OUTPATIENT", kind="drug")
    assert [o.order_uuid for o in opd] == [OPD_ORDER]
    assert opd[0].visit_uuid == VISIT
    assert opd[0].dose == 2.0


@respx.mock
async def test_active_lab_orders_use_testorder_and_drop_other_test_types() -> None:
    _login()
    lab = copy.deepcopy(_fixture("visit_orders.json")["encounters"][0]["orders"][1])
    rad = copy.deepcopy(_fixture("visit_orders.json")["encounters"][0]["orders"][2])
    for o in (lab, rad):
        o["encounter"] = {"uuid": ENC_1, "visit": {"uuid": VISIT}}
    route = respx.get(f"{REST}/order").mock(
        return_value=httpx.Response(200, json={"results": [lab, rad]})
    )
    orders = await _client().list_active_orders(PATIENT, care_setting="OUTPATIENT", kind="lab")
    assert route.calls.last.request.url.params["t"] == "testorder"
    # Radiology is also a test order; only the lab kind is returned.
    assert [o.order_uuid for o in orders] == [LAB]
    with pytest.raises(ValueError):
        await _client().list_active_orders(PATIENT, care_setting="OUTPATIENT", kind="other")


@respx.mock
async def test_active_orders_follow_next_page() -> None:
    _login()
    page = _fixture("drug_orders_active.json")
    first = {"results": page["results"][:1], "links": [{"rel": "next", "uri": "x"}]}
    second = {"results": page["results"][1:], "links": [{"rel": "prev", "uri": "x"}]}
    route = respx.get(f"{REST}/order").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    orders = await _client().list_active_orders(PATIENT, care_setting="INPATIENT", kind="drug")
    assert [o.order_uuid for o in orders] == [IPD_ORDER]
    assert route.calls[1].request.url.params["startIndex"] == "1"


@respx.mock
async def test_kind_from_configured_order_type_uuid() -> None:
    _login()
    body = _fixture("visit_orders.json")
    # Install renamed its order types; the configured uuids decide the kind.
    for enc in body["encounters"]:
        for o in enc["orders"]:
            o["orderType"]["name"] = "Renamed"
    _visit_route(body)
    client = _client(
        lab_order_type_uuid=LAB_TYPE,
        drug_order_type_uuid="a9922170-dd2d-42de-bb68-accb82da69d1",
    )
    kinds = {o.order_uuid: o.kind for o in await client.list_visit_orders(VISIT, active_only=False)}
    assert kinds[LAB] == "lab"
    assert kinds[NONCODED] == "drug"

    # A configured lab uuid wins over the name: a "Lab Order" of another type is "other".
    respx.reset()
    _login()
    _visit_route(_fixture("visit_orders.json"))
    client = _client(lab_order_type_uuid=RADIOLOGY_TYPE)
    kinds = {
        o.order_uuid: o.kind
        for o in await client.list_visit_orders(VISIT, kinds=frozenset({"lab", "other"}))
    }
    assert kinds[RADIOLOGY] == "lab"
    assert kinds[LAB] == "other"


@respx.mock
async def test_missing_visit_is_not_found() -> None:
    _login()
    respx.get(f"{REST}/visit/{VISIT}").mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(NotFound):
        await _client().list_visit_orders(VISIT)
