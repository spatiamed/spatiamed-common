import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_radiology import BahmniRadiologyClient, RadiologyConfig
from sm_common.integrations.exceptions import (
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.radiology_types import (
    PlacedRadiologyOrder,
    RadiologyOrderRow,
    RadiologyOrderWrite,
    RadiologyProcedure,
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
    "careSetting:(careSettingType),concept:(uuid))"
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
    with pytest.raises(VendorRejected, match="^order_number_too_long$"):
        await _client().place_radiology_orders(**PLACE_KW, orders=[RadiologyOrderWrite(XRAY_CHEST)])
    assert calls["n"] == 2


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
    *, order_type: str = RADIOLOGY_ORDER_TYPE, stopped: str | None = None, action: str = "NEW"
) -> dict[str, Any]:
    return {
        "uuid": ORDER_XRAY,
        "orderNumber": "ORD-1234",
        "action": action,
        "dateStopped": stopped,
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
    with pytest.raises(VendorRejected, match="not_a_radiology_order"):
        await _client().discontinue_radiology_order(**DISCONTINUE_KW)
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
