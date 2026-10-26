import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_radiology import BahmniRadiologyClient, RadiologyConfig
from sm_common.integrations.exceptions import ConfigError, NotFound, VendorRejected
from sm_common.integrations.radiology_types import RadiologyProcedure

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
