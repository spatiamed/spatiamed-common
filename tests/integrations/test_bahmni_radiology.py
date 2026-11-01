import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_radiology import (
    BahmniRadiologyClient,
    OrderNumberTooLong,
    RadiologyConfig,
)
from sm_common.integrations.exceptions import (
    ConfigError,
    DocumentFilingFailed,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.radiology_types import (
    PacsStudy,
    PlacedRadiologyOrder,
    RadiologyDocumentRef,
    RadiologyOrderRow,
    RadiologyOrderWrite,
    RadiologyProcedure,
    RadiologyReportOutcome,
)

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
FIX = Path(__file__).parent / "fixtures" / "bahmni" / "radiology"

ORDERABLE_SET = "80e69e73-255b-45e7-86fd-57a799a3bfca"
XRAY_CHEST = "b0fb2bb8-5f71-4811-8c87-9616d791dc3f"
CT_HEAD = "f36d8bc7-dec2-42b6-a564-7a641689702d"
US_ABDOMEN = "7bfda07b-121d-4494-a905-7a686252e406"
MRI_SET = "cebc9ec2-4fd2-4794-bf8d-6be513b051ae"
MRI_BRAIN = "c7099ba7-860b-4e12-b916-2add685e2def"
MRI_SPINE = "b707da86-a162-42db-93b4-f6d8c90b7dc4"

# The nested mappings group is last: OpenMRS's custom-rep parser breaks on anything after "))".
SET_REP = (
    "custom:(setMembers:(uuid,display,set,"
    "mappings:(conceptReferenceTerm:(code,conceptSource:(name)))))"
)

CONFIG = {
    "radiology_order_type_uuid": "0f2b1d55-9e0b-4c36-a1f6-0d1c7a0e4b11",
    "orderable_set_uuid": ORDERABLE_SET,
    "report_concept_uuid": "5a7b3c21-4d8e-4f90-a1b2-c3d4e5f60718",
    "report_encounter_type_uuid": "6b8c4d32-5e9f-4a01-b2c3-d4e5f6071829",
    "document_encounter_type_uuid": "7c9d5e43-6f0a-4b12-c3d4-e5f60718293a",
}


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text())
    return data


def _client(cfg: dict[str, Any] | None = None) -> BahmniRadiologyClient:
    return BahmniRadiologyClient(
        BahmniHttp(BASE, "u", "p"), RadiologyConfig.from_mapping(CONFIG if cfg is None else cfg)
    )


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _member(
    uuid: str, name: str, *, pacs: str | None = None, is_set: bool = False
) -> dict[str, Any]:
    mappings: list[dict[str, Any]] = [
        {"conceptReferenceTerm": {"code": "1234", "conceptSource": {"name": "SNOMED CT"}}}
    ]
    if pacs is not None:
        mappings.append(
            {
                "conceptReferenceTerm": {
                    "code": pacs,
                    "conceptSource": {"name": "PACS Procedure Code"},
                }
            }
        )
    return {"uuid": uuid, "display": name, "set": is_set, "mappings": mappings}


# --- config -----------------------------------------------------------------


def test_config_from_mapping_reads_readme_keys_and_ignores_viewer_go_live() -> None:
    cfg = RadiologyConfig.from_mapping(
        {**CONFIG, "viewer_go_live": {"date": "2026-01-01", "approved_by": "someone"}, "x": 1}
    )
    assert cfg.radiology_order_type_uuid == CONFIG["radiology_order_type_uuid"]
    assert cfg.orderable_set_uuid == ORDERABLE_SET
    assert cfg.report_concept_uuid == CONFIG["report_concept_uuid"]
    assert cfg.report_encounter_type_uuid == CONFIG["report_encounter_type_uuid"]
    assert cfg.document_encounter_type_uuid == CONFIG["document_encounter_type_uuid"]
    assert not hasattr(cfg, "viewer_go_live")


def test_config_defaults_empty_and_none_mapping() -> None:
    for cfg in (
        RadiologyConfig.from_mapping(None),
        RadiologyConfig.from_mapping({}),
        RadiologyConfig(),
    ):
        assert cfg.radiology_order_type_uuid == ""
        assert cfg.orderable_set_uuid == ""
        assert cfg.report_concept_uuid == ""
        assert cfg.report_encounter_type_uuid == ""
        assert cfg.document_encounter_type_uuid == ""


def test_config_non_string_value_is_config_error_naming_key() -> None:
    with pytest.raises(ConfigError, match="orderable_set_uuid"):
        RadiologyConfig.from_mapping({**CONFIG, "orderable_set_uuid": 12})


# --- list_procedures ----------------------------------------------------------


@respx.mock
async def test_list_procedures_flags_pacs_mapping() -> None:
    _login()
    route = respx.get(f"{REST}/concept/{ORDERABLE_SET}").mock(
        return_value=httpx.Response(200, json=_fixture("orderable_set.json"))
    )
    procs = await _client().list_procedures()
    assert procs == [
        RadiologyProcedure(
            concept_uuid=XRAY_CHEST,
            name="X-ray Chest PA",
            pacs_code="XR-CHEST-PA",
            pacs_mapped=True,
        ),
        RadiologyProcedure(
            concept_uuid=CT_HEAD, name="CT Head Plain", pacs_code="CT-HEAD", pacs_mapped=True
        ),
        RadiologyProcedure(
            concept_uuid=US_ABDOMEN, name="Ultrasound Abdomen", pacs_code=None, pacs_mapped=False
        ),
    ]
    assert route.call_count == 1
    assert route.calls[0].request.url.params["v"] == SET_REP


@respx.mock
async def test_list_procedures_filters_and_limits() -> None:
    _login()
    respx.get(f"{REST}/concept/{ORDERABLE_SET}").mock(
        return_value=httpx.Response(200, json=_fixture("orderable_set.json"))
    )
    client = _client()
    assert [p.concept_uuid for p in await client.list_procedures("  cHeSt ")] == [XRAY_CHEST]
    assert [p.concept_uuid for p in await client.list_procedures("ct")] == [CT_HEAD]
    assert [p.concept_uuid for p in await client.list_procedures(limit=2)] == [XRAY_CHEST, CT_HEAD]
    assert await client.list_procedures("mammogram") == []
    assert len(await client.list_procedures(limit=200)) == 3


@pytest.mark.parametrize("limit", [0, -1, 201])
async def test_list_procedures_limit_out_of_range_before_io(limit: int) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="limit"):
            await _client().list_procedures(limit=limit)
        assert mock.calls.call_count == 0


@respx.mock
async def test_list_procedures_nested_sets_flattened() -> None:
    _login()
    top = respx.get(f"{REST}/concept/{ORDERABLE_SET}").mock(
        return_value=httpx.Response(
            200,
            json={
                "setMembers": [
                    _member(XRAY_CHEST, "X-ray Chest PA", pacs="XR-CHEST-PA"),
                    _member(MRI_SET, "MRI procedures", is_set=True),
                    # The same procedure listed twice is returned once.
                    _member(XRAY_CHEST, "X-ray Chest PA", pacs="XR-CHEST-PA"),
                ]
            },
        )
    )
    nested = respx.get(f"{REST}/concept/{MRI_SET}").mock(
        return_value=httpx.Response(
            200,
            json={
                "setMembers": [
                    _member(MRI_BRAIN, "MRI Brain", pacs="MR-BRAIN"),
                    _member(MRI_SPINE, "MRI Spine"),
                    # A set that lists its parent again must not loop.
                    _member(ORDERABLE_SET, "Radiology orderables", is_set=True),
                ]
            },
        )
    )
    procs = await _client().list_procedures()
    assert [(p.concept_uuid, p.pacs_mapped) for p in procs] == [
        (XRAY_CHEST, True),
        (MRI_BRAIN, True),
        (MRI_SPINE, False),
    ]
    assert top.call_count == 1
    assert nested.call_count == 1
    assert nested.calls[0].request.url.params["v"] == SET_REP


@respx.mock
async def test_list_procedures_blank_pacs_code_is_unmapped() -> None:
    _login()
    respx.get(f"{REST}/concept/{ORDERABLE_SET}").mock(
        return_value=httpx.Response(
            200, json={"setMembers": [_member(MRI_SPINE, "MRI Spine", pacs="  ")]}
        )
    )
    [proc] = await _client().list_procedures()
    assert proc.pacs_code is None
    assert proc.pacs_mapped is False


async def test_missing_orderable_set_is_config_error_before_io() -> None:
    with respx.mock(assert_all_called=False) as mock:
        cfg = {k: v for k, v in CONFIG.items() if k != "orderable_set_uuid"}
        with pytest.raises(ConfigError, match="orderable_set_uuid"):
            await _client(cfg).list_procedures()
        assert mock.calls.call_count == 0


async def test_malformed_orderable_set_uuid_is_config_error_before_io() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ConfigError, match="orderable_set_uuid"):
            await _client({**CONFIG, "orderable_set_uuid": "../patient"}).list_procedures()
        assert mock.calls.call_count == 0


@respx.mock
async def test_list_procedures_404_is_not_found() -> None:
    _login()
    respx.get(f"{REST}/concept/{ORDERABLE_SET}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().list_procedures()


@respx.mock
async def test_list_procedures_malformed_body_is_vendor_rejected() -> None:
    _login()
    respx.get(f"{REST}/concept/{ORDERABLE_SET}").mock(
        return_value=httpx.Response(200, json={"setMembers": [{"display": "no uuid"}]})
    )
    with pytest.raises(VendorRejected):
        await _client().list_procedures()


# --- orders: place, find, discontinue ------------------------------------------

ENCOUNTER_URL = f"{REST}/bahmnicore/bahmniencounter"
RADIOLOGY_ORDER_TYPE = CONFIG["radiology_order_type_uuid"]
LAB_ORDER_TYPE = "4e4e16b2-bb71-4e68-a425-b3640f348876"
PATIENT = "3e6b15ff-0e45-476a-967a-fb78f2aee234"
VISIT = "bcdf607f-e497-468f-959c-02715c1d9760"
ENCOUNTER_TYPE = "77810ccf-5fca-4070-a717-2b8c0765ca23"
PROVIDER = "cfb24829-c378-4155-b79c-fc1284c0b25a"
LOCATION = "baf8831c-c8de-4b8c-a323-5296f2dba4cc"
ORDER_XRAY = "dbdd61b2-6a72-4cb1-aa5b-7a03e0f46ea9"
ORDER_CT = "b14937e2-5df6-41fd-ac7e-9cb38ff3175f"
ORDER_DC = "59d1c4b5-8391-44aa-a055-b04fb480e549"
UNKNOWN_CONCEPT = "6e67f008-1778-44e6-aafc-d5e729b9f092"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

READBACK_REP = "custom:(uuid,orderNumber,action,concept:(uuid),dateStopped)"
DISCONTINUE_READ_REP = (
    "custom:(uuid,orderNumber,action,dateStopped,orderType:(uuid),"
    "careSetting:(careSettingType),concept:(uuid),autoExpireDate)"
)
VISIT_ORDERS_REP = (
    "custom:(encounters:(orders:(uuid,orderNumber,action,dateActivated,dateStopped,"
    "orderType:(uuid),concept:(uuid))))"
)

PLACE_KW: dict[str, Any] = {
    "patient_uuid": PATIENT,
    "visit_uuid": VISIT,
    "encounter_type_uuid": ENCOUNTER_TYPE,
    "provider_uuid": PROVIDER,
    "location_uuid": LOCATION,
    "care_setting": "OUTPATIENT",
}
DISCONTINUE_KW: dict[str, Any] = {
    "order_uuid": ORDER_XRAY,
    "patient_uuid": PATIENT,
    "visit_uuid": VISIT,
    "encounter_type_uuid": ENCOUNTER_TYPE,
    "provider_uuid": PROVIDER,
    "location_uuid": LOCATION,
    "reason": "Ordered in error",
}


def _set_route() -> respx.Route:
    return respx.get(f"{REST}/concept/{ORDERABLE_SET}").mock(
        return_value=httpx.Response(200, json=_fixture("orderable_set.json"))
    )


def _echo_encounter(response_fixture: dict[str, Any] | None = None) -> Any:
    """A bahmniencounter mock that answers with the encounterUuid the request named."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        body = dict(response_fixture or {"orders": []})
        body["encounterUuid"] = sent["encounterUuid"]
        return httpx.Response(200, json=body)

    return handler


def _readback(uuid: str, concept: str, number: str) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "orderNumber": number,
        "action": "NEW",
        "concept": {"uuid": concept},
        "dateStopped": None,
    }


def _post_bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(c.request.content) for c in route.calls]


@respx.mock
async def test_place_orders_body_has_radiology_order_type_and_care_setting() -> None:
    _login()
    _set_route()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("encounter_with_orders_response.json"))
    )
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_XRAY, XRAY_CHEST, "ORD-1234"))
    )
    respx.get(f"{REST}/order/{ORDER_CT}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_CT, CT_HEAD, "ORD-1235"))
    )
    await _client().place_radiology_orders(
        **{**PLACE_KW, "care_setting": "INPATIENT"},
        orders=[
            RadiologyOrderWrite(XRAY_CHEST, comment_to_fulfiller="  synthetic indication "),
            RadiologyOrderWrite(CT_HEAD, urgency="STAT"),
        ],
    )
    [body] = _post_bodies(post)
    assert set(body) == {
        "encounterUuid",
        "patientUuid",
        "visitUuid",
        "locationUuid",
        "encounterTypeUuid",
        "providers",
        "orders",
    }
    assert UUID_RE.fullmatch(body["encounterUuid"])
    assert body["patientUuid"] == PATIENT
    assert body["visitUuid"] == VISIT
    assert body["locationUuid"] == LOCATION
    assert body["encounterTypeUuid"] == ENCOUNTER_TYPE
    assert body["providers"] == [{"uuid": PROVIDER}]
    assert body["orders"] == [
        {
            "concept": {"uuid": XRAY_CHEST},
            "orderTypeUuid": RADIOLOGY_ORDER_TYPE,
            "careSetting": "INPATIENT",
            "urgency": "ROUTINE",
            "commentToFulfiller": "synthetic indication",
        },
        {
            "concept": {"uuid": CT_HEAD},
            "orderTypeUuid": RADIOLOGY_ORDER_TYPE,
            "careSetting": "INPATIENT",
            "urgency": "STAT",
        },
    ]


@respx.mock
async def test_place_orders_each_call_names_a_fresh_encounter() -> None:
    _login()
    _set_route()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("encounter_with_orders_response.json"))
    )
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_XRAY, XRAY_CHEST, "ORD-1234"))
    )
    respx.get(f"{REST}/order/{ORDER_CT}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_CT, CT_HEAD, "ORD-1235"))
    )
    client = _client()
    for _ in range(2):
        await client.place_radiology_orders(
            **PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST), RadiologyOrderWrite(CT_HEAD)]
        )
    first, second = _post_bodies(post)
    assert first["encounterUuid"] != second["encounterUuid"]


@respx.mock
async def test_place_orders_returns_order_numbers_from_readback() -> None:
    _login()
    concept_set = _set_route()
    response = _fixture("encounter_with_orders_response.json")
    # The write response's order numbers are not trusted: the read-back is.
    response["orders"][0]["orderNumber"] = "ORD-9999"
    post = respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter(response))
    xray = respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_fixture("order_readback.json"))
    )
    ct = respx.get(f"{REST}/order/{ORDER_CT}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_CT, CT_HEAD, "ORD-1235"))
    )
    placed = await _client().place_radiology_orders(
        **PLACE_KW, orders=[RadiologyOrderWrite(CT_HEAD), RadiologyOrderWrite(XRAY_CHEST)]
    )
    sent = _post_bodies(post)[0]["encounterUuid"]
    assert placed == [
        PlacedRadiologyOrder(
            order_uuid=ORDER_CT, order_number="ORD-1235", concept_uuid=CT_HEAD, encounter_uuid=sent
        ),
        PlacedRadiologyOrder(
            order_uuid=ORDER_XRAY,
            order_number="ORD-1234",
            concept_uuid=XRAY_CHEST,
            encounter_uuid=sent,
        ),
    ]
    assert concept_set.call_count == 1
    assert xray.calls[0].request.url.params["v"] == READBACK_REP
    assert ct.calls[0].request.url.params["v"] == READBACK_REP


@respx.mock
async def test_place_orders_response_naming_another_encounter_is_landed_rejection() -> None:
    _login()
    _set_route()
    respx.post(ENCOUNTER_URL).mock(
        return_value=httpx.Response(200, json=_fixture("encounter_with_orders_response.json"))
    )
    with pytest.raises(VendorRejected) as exc:
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])
    assert exc.value.landed is True


@respx.mock
async def test_place_orders_readback_concept_mismatch_is_landed_rejection() -> None:
    _login()
    _set_route()
    respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("encounter_with_orders_response.json"))
    )
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_XRAY, CT_HEAD, "ORD-1234"))
    )
    with pytest.raises(VendorRejected) as exc:
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])
    assert exc.value.landed is True


@respx.mock
async def test_place_orders_order_missing_from_response_is_landed_rejection() -> None:
    _login()
    _set_route()
    respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter({"orders": []}))
    with pytest.raises(VendorRejected) as exc:
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])
    assert exc.value.landed is True


@respx.mock
async def test_unmapped_procedure_refused_by_default() -> None:
    _login()
    _set_route()
    # No POST route: any write would fail the test as an unmocked request.
    with pytest.raises(VendorRejected) as exc:
        await _client().place_radiology_orders(
            **PLACE_KW,
            orders=[RadiologyOrderWrite(XRAY_CHEST), RadiologyOrderWrite(US_ABDOMEN)],
        )
    assert str(exc.value) == "procedure_not_pacs_mapped"
    assert exc.value.code == "procedure_not_pacs_mapped"
    assert exc.value.landed is False
    assert [c.request.method for c in respx.calls] == ["GET", "GET"]


@respx.mock
async def test_unmapped_allowed_when_flag_set() -> None:
    _login()
    _set_route()
    response = _fixture("encounter_with_orders_response.json")
    response["orders"] = [{**response["orders"][0], "concept": {"uuid": US_ABDOMEN}}]
    post = respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter(response))
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_XRAY, US_ABDOMEN, "ORD-1240"))
    )
    [placed] = await _client().place_radiology_orders(
        **PLACE_KW, orders=[RadiologyOrderWrite(US_ABDOMEN)], allow_unmapped=True
    )
    assert placed.concept_uuid == US_ABDOMEN
    assert placed.order_number == "ORD-1240"
    assert post.call_count == 1


@pytest.mark.parametrize("allow_unmapped", [False, True])
@respx.mock
async def test_procedure_outside_orderable_set_refused(allow_unmapped: bool) -> None:
    _login()
    _set_route()
    with pytest.raises(VendorRejected) as exc:
        await _client().place_radiology_orders(
            **PLACE_KW,
            orders=[RadiologyOrderWrite(UNKNOWN_CONCEPT)],
            allow_unmapped=allow_unmapped,
        )
    assert str(exc.value) == "procedure_not_orderable"
    assert exc.value.code == "procedure_not_orderable"
    assert [c.request.method for c in respx.calls] == ["GET", "GET"]


@respx.mock
async def test_order_number_over_16_refused_and_discontinued() -> None:
    _login()
    _set_route()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("encounter_with_orders_response.json"))
    )
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(
            200, json=_readback(ORDER_XRAY, XRAY_CHEST, "ORD-12345678901234")
        )
    )
    with pytest.raises(VendorRejected) as exc:
        await _client().place_radiology_orders(
            **{**PLACE_KW, "care_setting": "INPATIENT"},
            orders=[RadiologyOrderWrite(XRAY_CHEST)],
        )
    assert str(exc.value) == "order_number_too_long"
    assert exc.value.code == "order_number_too_long"
    assert exc.value.landed is True
    place_body, dc_body = _post_bodies(post)
    assert dc_body["encounterUuid"] != place_body["encounterUuid"]
    assert UUID_RE.fullmatch(dc_body["encounterUuid"])
    for key in ("patientUuid", "visitUuid", "locationUuid", "encounterTypeUuid", "providers"):
        assert dc_body[key] == place_body[key]
    assert dc_body["orders"] == [
        {
            "concept": {"uuid": XRAY_CHEST},
            "orderTypeUuid": RADIOLOGY_ORDER_TYPE,
            "careSetting": "INPATIENT",
            "action": "DISCONTINUE",
            "previousOrderUuid": ORDER_XRAY,
            "orderReasonText": "accession too long",
        }
    ]


@respx.mock
async def test_only_overlong_orders_discontinued_and_all_placed_attached() -> None:
    _login()
    _set_route()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("encounter_with_orders_response.json"))
    )
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(
            200, json=_readback(ORDER_XRAY, XRAY_CHEST, "ORD-12345678901234")
        )
    )
    respx.get(f"{REST}/order/{ORDER_CT}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_CT, CT_HEAD, "ORD-1235"))
    )
    with pytest.raises(OrderNumberTooLong) as exc:
        await _client().place_radiology_orders(
            **PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST), RadiologyOrderWrite(CT_HEAD)]
        )
    assert exc.value.landed is True
    assert [p.order_uuid for p in exc.value.placed] == [ORDER_XRAY, ORDER_CT]
    assert [p.order_uuid for p in exc.value.too_long] == [ORDER_XRAY]
    assert exc.value.discontinued == [ORDER_XRAY]
    _, dc_body = _post_bodies(post)
    assert [o["previousOrderUuid"] for o in dc_body["orders"]] == [ORDER_XRAY]


@respx.mock
async def test_order_number_of_exactly_16_accepted() -> None:
    _login()
    _set_route()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("encounter_with_orders_response.json"))
    )
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_readback(ORDER_XRAY, XRAY_CHEST, "ORD-123456789012"))
    )
    [placed] = await _client().place_radiology_orders(
        **PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)]
    )
    assert placed.order_number == "ORD-123456789012"
    assert post.call_count == 1


@respx.mock
async def test_order_number_too_long_still_raised_when_discontinue_fails() -> None:
    _login()
    _set_route()
    calls = {"n": 0}
    place = _echo_encounter(_fixture("encounter_with_orders_response.json"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return place(request) if calls["n"] == 1 else httpx.Response(500)

    respx.post(ENCOUNTER_URL).mock(side_effect=handler)
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(
            200, json=_readback(ORDER_XRAY, XRAY_CHEST, "ORD-12345678901234")
        )
    )
    with pytest.raises(VendorRejected, match="^order_number_too_long$") as exc:
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])
    assert calls["n"] == 2
    assert isinstance(exc.value, OrderNumberTooLong)
    assert exc.value.discontinued == []


@pytest.mark.parametrize("status", [500, 429])
@respx.mock
async def test_readback_transient_error_after_place_is_landed(status: int) -> None:
    _login()
    _set_route()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("encounter_with_orders_response.json"))
    )
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(return_value=httpx.Response(status))
    with pytest.raises(TransientError) as exc:
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])
    assert exc.value.landed is True
    assert exc.value.status_code == status
    assert post.call_count == 1


@respx.mock
async def test_readback_non_json_after_place_is_landed() -> None:
    _login()
    _set_route()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("encounter_with_orders_response.json"))
    )
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, content=b"<html>not json</html>")
    )
    with pytest.raises(VendorRejected) as exc:
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])
    assert exc.value.landed is True
    assert post.call_count == 1


@respx.mock
async def test_place_500_is_transient_not_retried() -> None:
    _login()
    _set_route()
    post = respx.post(ENCOUNTER_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(TransientError):
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])
    assert post.call_count == 1


@respx.mock
async def test_place_404_is_not_found() -> None:
    _login()
    _set_route()
    respx.post(ENCOUNTER_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])


@pytest.mark.parametrize(
    ("kw", "orders"),
    [
        ({}, []),
        ({"patient_uuid": "../patient"}, [RadiologyOrderWrite(XRAY_CHEST)]),
        ({"visit_uuid": ""}, [RadiologyOrderWrite(XRAY_CHEST)]),
        ({"encounter_type_uuid": "a b"}, [RadiologyOrderWrite(XRAY_CHEST)]),
        ({"provider_uuid": "x/y"}, [RadiologyOrderWrite(XRAY_CHEST)]),
        ({"location_uuid": "?"}, [RadiologyOrderWrite(XRAY_CHEST)]),
        ({"care_setting": "EMERGENCY"}, [RadiologyOrderWrite(XRAY_CHEST)]),
        ({}, [RadiologyOrderWrite("not a uuid")]),
        ({}, [RadiologyOrderWrite(XRAY_CHEST, urgency="ASAP")]),  # type: ignore[arg-type]
        ({}, [RadiologyOrderWrite(XRAY_CHEST, comment_to_fulfiller="x" * 1025)]),
        ({}, [RadiologyOrderWrite(XRAY_CHEST), RadiologyOrderWrite(XRAY_CHEST)]),
    ],
)
async def test_place_orders_bad_input_is_value_error_before_io(
    kw: dict[str, Any], orders: list[RadiologyOrderWrite]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await _client().place_radiology_orders(**{**PLACE_KW, **kw}, orders=orders)
        assert mock.calls.call_count == 0


@pytest.mark.parametrize("key", ["radiology_order_type_uuid", "orderable_set_uuid"])
async def test_place_orders_missing_config_is_config_error_before_io(key: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        cfg = {k: v for k, v in CONFIG.items() if k != key}
        with pytest.raises(ConfigError, match=key):
            await _client(cfg).place_radiology_orders(
                **PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)]
            )
        assert mock.calls.call_count == 0


@respx.mock
async def test_find_orders_by_visit_filters_radiology_type() -> None:
    _login()
    respx.get(f"{REST}/visit/{VISIT}").mock(
        return_value=httpx.Response(200, json=_fixture("visit_orders.json"))
    )
    rows = await _client().find_radiology_orders(VISIT)
    assert rows == [
        RadiologyOrderRow(
            order_uuid=ORDER_XRAY,
            order_number="ORD-1234",
            concept_uuid=XRAY_CHEST,
            action="NEW",
            date_activated=datetime(2026, 3, 2, 4, 45, tzinfo=timezone.utc),
            date_stopped=datetime(2026, 3, 2, 5, 30, tzinfo=timezone.utc),
        ),
        RadiologyOrderRow(
            order_uuid=ORDER_DC,
            order_number="ORD-1236",
            concept_uuid=XRAY_CHEST,
            action="DISCONTINUE",
            date_activated=datetime(2026, 3, 2, 5, 30, tzinfo=timezone.utc),
            date_stopped=None,
        ),
        RadiologyOrderRow(
            order_uuid=ORDER_CT,
            order_number="ORD-1237",
            concept_uuid=CT_HEAD,
            action="NEW",
            date_activated=datetime(2026, 3, 2, 5, 35, tzinfo=timezone.utc),
            date_stopped=None,
        ),
    ]


@respx.mock
async def test_find_orders_sorted_oldest_first_across_encounters() -> None:
    _login()
    body = _fixture("visit_orders.json")
    body["encounters"].reverse()
    respx.get(f"{REST}/visit/{VISIT}").mock(return_value=httpx.Response(200, json=body))
    rows = await _client().find_radiology_orders(VISIT)
    assert [r.order_uuid for r in rows] == [ORDER_XRAY, ORDER_DC, ORDER_CT]


@respx.mock
async def test_find_orders_custom_rep_keeps_nested_group_last() -> None:
    _login()
    route = respx.get(f"{REST}/visit/{VISIT}").mock(
        return_value=httpx.Response(200, json={"encounters": []})
    )
    assert await _client().find_radiology_orders(VISIT) == []
    rep = route.calls[0].request.url.params["v"]
    assert rep == VISIT_ORDERS_REP
    # Nothing may follow a "))" in an OpenMRS custom representation.
    assert rep.endswith("concept:(uuid))))")
    assert "))," not in rep
    # A generic order read must not name drug-order-only fields.
    assert "drugNonCoded" not in rep and "dosing" not in rep


@respx.mock
async def test_find_orders_visit_404_is_not_found() -> None:
    _login()
    respx.get(f"{REST}/visit/{VISIT}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().find_radiology_orders(VISIT)


@respx.mock
async def test_find_orders_malformed_row_is_vendor_rejected() -> None:
    _login()
    row = _fixture("visit_orders.json")["encounters"][0]["orders"][0]
    respx.get(f"{REST}/visit/{VISIT}").mock(
        return_value=httpx.Response(
            200, json={"encounters": [{"orders": [{**row, "dateActivated": None}]}]}
        )
    )
    with pytest.raises(VendorRejected):
        await _client().find_radiology_orders(VISIT)


async def test_find_orders_bad_input_and_missing_config_before_io() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await _client().find_radiology_orders("../x")
        cfg = {k: v for k, v in CONFIG.items() if k != "radiology_order_type_uuid"}
        with pytest.raises(ConfigError, match="radiology_order_type_uuid"):
            await _client(cfg).find_radiology_orders(VISIT)
        assert mock.calls.call_count == 0


def _order_read(
    *,
    order_type: str = RADIOLOGY_ORDER_TYPE,
    stopped: str | None = None,
    action: str = "NEW",
    expires: str | None = None,
) -> dict[str, Any]:
    return {
        "uuid": ORDER_XRAY,
        "orderNumber": "ORD-1234",
        "action": action,
        "dateStopped": stopped,
        "autoExpireDate": expires,
        "orderType": {"uuid": order_type},
        "careSetting": {"careSettingType": "OUTPATIENT"},
        "concept": {"uuid": XRAY_CHEST},
    }


@respx.mock
async def test_discontinue_body_shape_and_reason_required() -> None:
    _login()
    read = respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_order_read())
    )
    post = respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter())
    assert await _client().discontinue_radiology_order(**DISCONTINUE_KW) is None
    assert read.calls[0].request.url.params["v"] == DISCONTINUE_READ_REP
    [body] = _post_bodies(post)
    assert UUID_RE.fullmatch(body.pop("encounterUuid"))
    assert body == {
        "patientUuid": PATIENT,
        "visitUuid": VISIT,
        "locationUuid": LOCATION,
        "encounterTypeUuid": ENCOUNTER_TYPE,
        "providers": [{"uuid": PROVIDER}],
        "orders": [
            {
                "concept": {"uuid": XRAY_CHEST},
                "orderTypeUuid": RADIOLOGY_ORDER_TYPE,
                "careSetting": "OUTPATIENT",
                "action": "DISCONTINUE",
                "previousOrderUuid": ORDER_XRAY,
                "orderReasonText": "Ordered in error",
            }
        ],
    }

    for bad in ("", "   ", "x" * 256):
        with pytest.raises(ValueError, match="reason"):
            await _client().discontinue_radiology_order(**{**DISCONTINUE_KW, "reason": bad})
    assert read.call_count == 1
    assert post.call_count == 1


@respx.mock
async def test_discontinue_non_radiology_order_refused() -> None:
    _login()
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_order_read(order_type=LAB_ORDER_TYPE))
    )
    post = respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter())
    with pytest.raises(VendorRejected, match="^not_a_radiology_order$") as exc:
        await _client().discontinue_radiology_order(**DISCONTINUE_KW)
    assert exc.value.code == "not_a_radiology_order"
    assert post.call_count == 0


@respx.mock
async def test_discontinue_already_stopped_is_noop() -> None:
    _login()
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_order_read(stopped="2026-03-02T11:00:00.000+0530"))
    )
    post = respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter())
    await _client().discontinue_radiology_order(**DISCONTINUE_KW)
    assert post.call_count == 0


@respx.mock
async def test_discontinue_auto_expired_order_is_noop() -> None:
    _login()
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_order_read(expires="2020-01-01T10:00:00.000+0530"))
    )
    post = respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter())
    await _client().discontinue_radiology_order(**DISCONTINUE_KW)
    assert post.call_count == 0


@respx.mock
async def test_discontinue_order_expiring_later_is_discontinued() -> None:
    _login()
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_order_read(expires="2999-01-01T10:00:00.000+0530"))
    )
    post = respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter())
    await _client().discontinue_radiology_order(**DISCONTINUE_KW)
    assert post.call_count == 1


@respx.mock
async def test_discontinue_a_discontinue_order_refused() -> None:
    _login()
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(
        return_value=httpx.Response(200, json=_order_read(action="DISCONTINUE"))
    )
    post = respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter())
    with pytest.raises(VendorRejected):
        await _client().discontinue_radiology_order(**DISCONTINUE_KW)
    assert post.call_count == 0


@respx.mock
async def test_discontinue_missing_order_is_not_found() -> None:
    _login()
    respx.get(f"{REST}/order/{ORDER_XRAY}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().discontinue_radiology_order(**DISCONTINUE_KW)


# --- reports ----------------------------------------------------------------

OBS_URL = f"{REST}/obs"
REPORT_OBS = "0d6f1c2e-91a3-4f7b-8e52-3c1a9b7d6e40"
REPORT_AT = datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc)
REPORT_KW: dict[str, Any] = {
    "order_uuid": ORDER_XRAY,
    "patient_uuid": PATIENT,
    "visit_uuid": VISIT,
    "provider_uuid": PROVIDER,
    "location_uuid": LOCATION,
    "report_at": REPORT_AT,
    "findings": "No focal lesion.",
    "impression": "Normal study.",
}


@respx.mock
async def test_report_obs_carries_order_uuid() -> None:
    _login()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("report_encounter_response.json"))
    )
    out = await _client().write_radiology_report(**REPORT_KW)
    [body] = _post_bodies(post)
    assert UUID_RE.fullmatch(body["encounterUuid"])
    assert body["encounterTypeUuid"] == CONFIG["report_encounter_type_uuid"]
    assert body["patientUuid"] == PATIENT
    assert body["visitUuid"] == VISIT
    assert body["locationUuid"] == LOCATION
    assert body["providers"] == [{"uuid": PROVIDER}]
    # The server stamps the encounter; the report time rides on the obs.
    assert "encounterDateTime" not in body
    assert body.get("orders", []) == []
    [obs] = body["observations"]
    assert obs["orderUuid"] == ORDER_XRAY
    assert obs["concept"] == {"uuid": CONFIG["report_concept_uuid"]}
    assert obs["observationDateTime"] == int(REPORT_AT.timestamp() * 1000)
    assert out == RadiologyReportOutcome(encounter_uuid=body["encounterUuid"], obs_uuid=REPORT_OBS)


@respx.mock
async def test_report_text_has_fixed_headings() -> None:
    _login()
    post = respx.post(ENCOUNTER_URL).mock(
        side_effect=_echo_encounter(_fixture("report_encounter_response.json"))
    )
    await _client().write_radiology_report(
        **{**REPORT_KW, "findings": "  No focal lesion.\n", "impression": "\nNormal study.  "}
    )
    [body] = _post_bodies(post)
    assert body["observations"][0]["value"] == (
        "Findings:\nNo focal lesion.\n\nImpression:\nNormal study."
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("impression", ""),
        ("impression", "   \n "),
        ("impression", "x" * 20_001),
        ("findings", ""),
        ("findings", "x" * 20_001),
        ("findings", None),
    ],
)
async def test_report_empty_impression_is_value_error(field: str, value: Any) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match=field):
            await _client().write_radiology_report(**{**REPORT_KW, field: value})
        assert mock.calls.call_count == 0


@pytest.mark.parametrize(
    ("override", "exc"),
    [
        ({"order_uuid": ""}, ValueError),
        ({"order_uuid": "../obs"}, ValueError),
        ({"report_at": datetime(2026, 10, 20, 9, 30)}, ValueError),
    ],
)
async def test_report_bad_input_refused_before_io(
    override: dict[str, Any], exc: type[Exception]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(exc):
            await _client().write_radiology_report(**{**REPORT_KW, **override})
        assert mock.calls.call_count == 0


@pytest.mark.parametrize("key", ["report_concept_uuid", "report_encounter_type_uuid"])
async def test_report_missing_config_is_config_error_before_io(key: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ConfigError, match=key):
            await _client({**CONFIG, key: ""}).write_radiology_report(**REPORT_KW)
        assert mock.calls.call_count == 0


@respx.mock
async def test_report_response_without_the_order_obs_is_landed() -> None:
    _login()
    response = _fixture("report_encounter_response.json")
    # Only a voided obs and an obs for another order come back.
    response["observations"] = [
        {**response["observations"][0], "voided": True},
        {**response["observations"][0], "uuid": ORDER_DC, "orderUuid": ORDER_CT},
    ]
    respx.post(ENCOUNTER_URL).mock(side_effect=_echo_encounter(response))
    with pytest.raises(VendorRejected) as info:
        await _client().write_radiology_report(**REPORT_KW)
    assert info.value.landed is True


@respx.mock
async def test_report_500_is_transient_not_retried() -> None:
    _login()
    post = respx.post(ENCOUNTER_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(TransientError):
        await _client().write_radiology_report(**REPORT_KW)
    assert post.call_count == 1


@respx.mock
async def test_void_report_sends_reason() -> None:
    _login()
    route = respx.delete(f"{OBS_URL}/{REPORT_OBS}").mock(return_value=httpx.Response(204))
    await _client().void_radiology_report(REPORT_OBS, "Amended: laterality corrected")
    assert route.call_count == 1
    assert route.calls[0].request.url.params["reason"] == "Amended: laterality corrected"


@respx.mock
async def test_void_report_unknown_obs_is_not_found() -> None:
    _login()
    respx.delete(f"{OBS_URL}/{REPORT_OBS}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().void_radiology_report(REPORT_OBS, "Amended")


@pytest.mark.parametrize(
    ("obs_uuid", "reason"),
    [(REPORT_OBS, ""), (REPORT_OBS, "  "), (REPORT_OBS, "x" * 256), ("a/b", "r")],
)
async def test_void_report_bad_input_before_io(obs_uuid: str, reason: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await _client().void_radiology_report(obs_uuid, reason)
        assert mock.calls.call_count == 0


# --- image studies ----------------------------------------------------------

PACS_URL = f"{REST}/pacs/studies"


@respx.mock
async def test_pacs_studies_parsed_with_accession_stripped() -> None:
    _login()
    route = respx.get(PACS_URL).mock(
        return_value=httpx.Response(200, json=_fixture("pacs_studies.json"))
    )
    studies = await _client().list_pacs_studies("LT-P4-0001", date=date(2026, 10, 20))
    params = route.calls[0].request.url.params
    assert params["patientId"] == "LT-P4-0001"
    assert params["date"] == "20261020"  # DICOM DA
    assert studies == [
        PacsStudy(
            study_instance_uid="1.2.826.0.1.3680043.8.498.10000000000000000001",
            accession="ORD-1234",
            modality=None,
            study_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            description=None,
        ),
        PacsStudy(
            study_instance_uid="1.2.826.0.1.3680043.8.498.10000000000000000002",
            accession=None,
            modality="CT",
            study_at=datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc),
            description="CT Head Plain",
        ),
    ]


@respx.mock
async def test_pacs_studies_without_date_sends_no_date_and_empty_list_is_empty() -> None:
    _login()
    route = respx.get(PACS_URL).mock(return_value=httpx.Response(200, json=[]))
    assert await _client().list_pacs_studies("LT-P4-0001") == []
    assert "date" not in route.calls[0].request.url.params


@respx.mock
async def test_pacs_501_is_config_error() -> None:
    _login()
    respx.get(PACS_URL).mock(
        return_value=httpx.Response(
            501, json={"error": {"message": "PACS Server configuration is not defined"}}
        )
    )
    with pytest.raises(ConfigError, match="pacs_not_configured") as info:
        await _client().list_pacs_studies("LT-P4-0001")
    assert info.value.code == "pacs_not_configured"


@pytest.mark.parametrize("status", [500, 502, 503])
@respx.mock
async def test_pacs_500_is_transient(status: int) -> None:
    _login()
    respx.get(PACS_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(TransientError) as info:
        await _client().list_pacs_studies("LT-P4-0001")
    assert not isinstance(info.value, ConfigError)


@respx.mock
async def test_pacs_404_is_not_found() -> None:
    _login()
    respx.get(PACS_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().list_pacs_studies("LT-P4-0001")


@respx.mock
async def test_pacs_non_list_response_is_vendor_rejected() -> None:
    _login()
    respx.get(PACS_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    with pytest.raises(VendorRejected):
        await _client().list_pacs_studies("LT-P4-0001")


@pytest.mark.parametrize("identifier", ["", "   ", "x" * 65, "LT\x00P4"])
async def test_pacs_bad_identifier_before_io(identifier: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await _client().list_pacs_studies(identifier)
        assert mock.calls.call_count == 0


# --- documents --------------------------------------------------------------

UPLOAD_URL = f"{REST}/bahmnicore/visitDocument/uploadDocument"
DOC_URL = f"{REST}/bahmnicore/visitDocument"
VISIT_TYPE = "e5a3f1b2-8c4d-4e6f-a0b1-c2d3e4f50617"
TEST_CONCEPT = XRAY_CHEST
DOC_ENCOUNTER = "4b1c9e7a-2f3d-4a5b-8c6d-7e8f9a0b1c2d"
PDF = b"%PDF-1.4\n% synthetic one-page film report\n%%EOF\n"
TAKEN_AT = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)
UPLOAD_KW: dict[str, Any] = {
    "patient_uuid": PATIENT,
    "visit_uuid": VISIT,
    "visit_type_uuid": VISIT_TYPE,
    "provider_uuid": PROVIDER,
    "location_uuid": LOCATION,
    "test_concept_uuid": TEST_CONCEPT,
    "content": PDF,
    "mime_type": "application/pdf",
    "file_name": "LT-P4-film.pdf",
    "taken_at": TAKEN_AT,
    "comment": "Outside film, synthetic",
}


@respx.mock
async def test_upload_two_step_bodies() -> None:
    _login()
    url = _fixture("upload_document_response.json")["url"]
    up = respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(200, json=_fixture("upload_document_response.json"))
    )
    filed = respx.post(DOC_URL).mock(
        return_value=httpx.Response(200, json={"visitUuid": VISIT, "encounterUuid": DOC_ENCOUNTER})
    )
    ref = await _client().upload_radiology_document(**UPLOAD_KW)
    [first] = _post_bodies(up)
    assert first == {
        "content": base64.b64encode(PDF).decode("ascii"),
        "format": "pdf",
        "fileType": "pdf",
        "patientUuid": PATIENT,
        "encounterTypeName": "RADIOLOGY",
        "fileName": "LT-P4-film.pdf",
    }
    [second] = _post_bodies(filed)
    at = int(TAKEN_AT.timestamp() * 1000)
    assert second == {
        "patientUuid": PATIENT,
        "visitUuid": VISIT,
        "visitTypeUuid": VISIT_TYPE,
        "visitStartDate": at,
        "visitEndDate": at,
        "encounterTypeUuid": CONFIG["document_encounter_type_uuid"],
        "encounterDateTime": at,
        "providerUuid": PROVIDER,
        "locationUuid": LOCATION,
        "documents": [
            {
                "testUuid": TEST_CONCEPT,
                "image": url,
                "obsDateTime": at,
                "comment": "Outside film, synthetic",
            }
        ],
    }
    assert ref == RadiologyDocumentRef(encounter_uuid=DOC_ENCOUNTER, file_url=url)


@respx.mock
async def test_upload_image_kinds_and_no_comment() -> None:
    _login()
    up = respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json={"url": "a/b.png"}))
    filed = respx.post(DOC_URL).mock(
        return_value=httpx.Response(200, json={"visitUuid": VISIT, "encounterUuid": DOC_ENCOUNTER})
    )
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    await _client().upload_radiology_document(
        **{**UPLOAD_KW, "content": png, "mime_type": "image/png", "comment": None}
    )
    [first] = _post_bodies(up)
    assert (first["format"], first["fileType"]) == ("png", "image")
    [second] = _post_bodies(filed)
    assert "comment" not in second["documents"][0]


@respx.mock
async def test_upload_file_name_is_sanitised() -> None:
    _login()
    up = respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json={"url": "a/b.pdf"}))
    respx.post(DOC_URL).mock(
        return_value=httpx.Response(200, json={"visitUuid": VISIT, "encounterUuid": DOC_ENCOUNTER})
    )
    await _client().upload_radiology_document(
        **{**UPLOAD_KW, "file_name": "../../etc/LT P4 film.pdf"}
    )
    name = _post_bodies(up)[0]["fileName"]
    assert "/" not in name and ".." not in name and " " not in name
    assert name.endswith("LT-P4-film.pdf")


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"content": PDF + b"x" * 64, "max_bytes": 32}, "max_bytes"),
        ({"mime_type": "image/gif"}, "mime"),
        ({"mime_type": "application/dicom"}, "mime"),
        ({"content": b""}, "empty"),
        ({"content": b"GIF89a not a pdf"}, "content"),
        ({"max_bytes": 0}, "max_bytes"),
        ({"file_name": "   "}, "file_name"),
        ({"file_name": ".."}, "file_name"),
        ({"taken_at": datetime(2026, 10, 19, 6, 0)}, "taken_at"),
        ({"comment": "x" * 1025}, "comment"),
        ({"visit_uuid": "a b"}, "visit_uuid"),
        ({"test_concept_uuid": ""}, "test_concept_uuid"),
    ],
)
async def test_upload_rejects_oversize_and_bad_mime_before_io(
    override: dict[str, Any], match: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match=match):
            await _client().upload_radiology_document(**{**UPLOAD_KW, **override})
        assert mock.calls.call_count == 0


async def test_upload_default_cap_is_ten_mib() -> None:
    big = b"%PDF" + b"\x00" * (10 * 1024 * 1024 - 3)
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="max_bytes"):
            await _client().upload_radiology_document(**{**UPLOAD_KW, "content": big})
        assert mock.calls.call_count == 0


async def test_upload_missing_document_encounter_type_is_config_error_before_io() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ConfigError, match="document_encounter_type_uuid"):
            await _client({**CONFIG, "document_encounter_type_uuid": ""}).upload_radiology_document(
                **UPLOAD_KW
            )
        assert mock.calls.call_count == 0


@respx.mock
async def test_upload_filing_failure_carries_uploaded_url() -> None:
    _login()
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json={"url": "a/b.pdf"}))
    filed = respx.post(DOC_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(DocumentFilingFailed) as info:
        await _client().upload_radiology_document(**UPLOAD_KW)
    assert info.value.uploaded_url == "a/b.pdf"
    assert info.value.landed is True
    assert filed.call_count == 1


@respx.mock
async def test_upload_filing_response_without_encounter_is_landed() -> None:
    _login()
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json={"url": "a/b.pdf"}))
    respx.post(DOC_URL).mock(return_value=httpx.Response(200, json={"visitUuid": VISIT}))
    with pytest.raises(DocumentFilingFailed) as info:
        await _client().upload_radiology_document(**UPLOAD_KW)
    assert info.value.landed is True


@respx.mock
async def test_upload_step_one_without_url_files_nothing() -> None:
    _login()
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json={}))
    filed = respx.post(DOC_URL).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(VendorRejected) as info:
        await _client().upload_radiology_document(**UPLOAD_KW)
    assert not isinstance(info.value, DocumentFilingFailed)
    assert filed.call_count == 0
