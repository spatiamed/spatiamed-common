import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_lab_results import (
    BahmniLabResultsClient,
    LabResultsConfig,
)
from sm_common.integrations.exceptions import (
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.lab_results_types import LabResultRow, LabTestDefinition

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
LAB_RESULTS = f"{REST}/bahmnicore/labOrderResults"
CONCEPT = f"{REST}/concept"
FIX = Path(__file__).parent / "fixtures" / "bahmni" / "lab_results"

PATIENT = "deeeac16-ad7f-49f6-96b3-cd6f06196e59"
VISIT_A = "fc17aded-c4d6-4bd9-890b-a8e684a33ffe"
VISIT_B = "bc1cd587-50be-4e3e-a4c1-95679e8bb3ba"
ACCESSION = "00000000-0000-4000-a000-000000000001"
ORDER_HB = "5086fb24-30d7-473c-8fb2-9ea6a784a7eb"
ORDER_URINE = "92b6fe94-d91f-426a-952c-521c24fcc473"
ORDER_BG = "033365b3-bd55-4aff-b173-bbe66d1935e6"
ORDER_PANEL = "ecc6d325-5533-4c72-8dca-a3a72f4452c8"
ORDER_CREAT = "c4f5c422-9719-434f-9d57-cf8209b6a534"
ORDER_TSH = "bda04d45-d11f-4585-8a8d-e5e6565f0a4b"
TEST_HB = "6dd34379-e216-49e4-89e8-e9e356cd31d6"
TEST_BG = "01bed5d9-00aa-4797-819f-501d689546a5"
PANEL = "79268774-5fe8-4802-bb27-352a2837b9bc"
ANSWER_B_POS = "d380105e-05b1-4503-b1ef-51151f63692c"
ANSWER_O_POS = "0e98298a-9898-47fb-a953-781e7d088d58"

CONFIG_KEYS = (
    "lab_result_encounter_type_uuid",
    "concept_lab_abnormal_uuid",
    "concept_lab_minnormal_uuid",
    "concept_lab_maxnormal_uuid",
    "concept_lab_notes_uuid",
    "concept_referred_out_uuid",
    "result_location_uuid",
)


def _fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def _client() -> BahmniLabResultsClient:
    return BahmniLabResultsClient(BahmniHttp(BASE, "u", "p"), LabResultsConfig())


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _rows_by_order(rows: list[LabResultRow]) -> dict[str, list[LabResultRow]]:
    out: dict[str, list[LabResultRow]] = {}
    for row in rows:
        out.setdefault(row.order_uuid, []).append(row)
    return out


async def _read_mixed() -> list[LabResultRow]:
    _login()
    respx.get(LAB_RESULTS).mock(
        return_value=httpx.Response(200, json=_fixture("lab_order_results_mixed.json"))
    )
    return await _client().read_lab_results(PATIENT, number_of_visits=1)


# --- config -----------------------------------------------------------------


def test_config_from_mapping_reads_readme_keys() -> None:
    values = {k: f"{k}-value" for k in CONFIG_KEYS}
    cfg = LabResultsConfig.from_mapping({**values, "unrelated_key": 7})
    for k in CONFIG_KEYS:
        assert getattr(cfg, k) == f"{k}-value"

    empty = LabResultsConfig.from_mapping(None)
    for k in CONFIG_KEYS:
        assert getattr(empty, k) == ""
    assert LabResultsConfig.from_mapping({}) == empty


def test_config_non_string_value_is_config_error() -> None:
    with pytest.raises(ConfigError, match="concept_lab_notes_uuid"):
        LabResultsConfig.from_mapping({"concept_lab_notes_uuid": 42})
    with pytest.raises(ConfigError, match="result_location_uuid"):
        LabResultsConfig.from_mapping({"result_location_uuid": None})


# --- read: query shape ------------------------------------------------------


@respx.mock
async def test_read_by_patient_sends_patient_uuid_and_number_of_visits() -> None:
    _login()
    route = respx.get(LAB_RESULTS).mock(
        return_value=httpx.Response(200, json={"results": [], "tabularResult": {}})
    )
    assert await _client().read_lab_results(PATIENT, number_of_visits=3) == []
    params = route.calls.last.request.url.params
    assert params.get_list("patientUuid") == [PATIENT]
    assert params.get_list("numberOfVisits") == ["3"]
    assert "visitUuids" not in params


@respx.mock
async def test_read_by_patient_without_number_of_visits_sends_patient_only() -> None:
    _login()
    route = respx.get(LAB_RESULTS).mock(return_value=httpx.Response(200, json={"results": []}))
    await _client().read_lab_results(PATIENT)
    params = route.calls.last.request.url.params
    assert dict(params) == {"patientUuid": PATIENT}


@respx.mock
async def test_read_by_visits_repeats_visit_uuids_param() -> None:
    _login()
    route = respx.get(LAB_RESULTS).mock(return_value=httpx.Response(200, json={"results": []}))
    await _client().read_lab_results(PATIENT, visit_uuids=[VISIT_A, VISIT_B, VISIT_A])
    params = route.calls.last.request.url.params
    assert params.get_list("visitUuids") == [VISIT_A, VISIT_B]
    # Bahmni's visit variant takes no patientUuid (it uses the first visit's patient).
    assert "patientUuid" not in params
    assert "numberOfVisits" not in params


@respx.mock
async def test_visit_uuids_and_number_of_visits_together_is_value_error() -> None:
    route = respx.route()
    client = _client()
    with pytest.raises(ValueError, match="mutually exclusive"):
        await client.read_lab_results(PATIENT, visit_uuids=[VISIT_A], number_of_visits=1)
    with pytest.raises(ValueError):
        await client.read_lab_results(PATIENT, visit_uuids=[])
    with pytest.raises(ValueError):
        await client.read_lab_results(PATIENT, visit_uuids=["not a uuid/../x"])
    with pytest.raises(ValueError):
        await client.read_lab_results("bad uuid?")
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            await client.read_lab_results(PATIENT, number_of_visits=bad)
    assert route.call_count == 0


# --- read: parsing ----------------------------------------------------------


@respx.mock
async def test_numeric_abnormal_row_parsed() -> None:
    rows = _rows_by_order(await _read_mixed())
    [hb] = rows[ORDER_HB]
    assert hb.status == "resulted"
    assert hb.test_uuid == TEST_HB
    assert hb.test_name == "Test Haemoglobin"
    assert hb.accession_uuid == ACCESSION
    assert hb.unit == "g/dL"
    assert hb.result == "11.2"
    assert hb.min_normal == Decimal("13.0")
    assert hb.max_normal == Decimal("17.0")
    assert isinstance(hb.min_normal, Decimal)
    assert hb.abnormal is True
    assert hb.referred_out is False
    assert hb.provider == "Test Provider"
    assert hb.panel_uuid is None and hb.panel_name is None
    assert hb.result_at is not None and hb.result_at.tzinfo is not None
    assert hb.result_at == datetime(2026, 10, 20, 4, 45, tzinfo=timezone.utc)


@respx.mock
async def test_text_and_coded_rows_parsed() -> None:
    rows = _rows_by_order(await _read_mixed())
    [urine] = rows[ORDER_URINE]
    assert urine.status == "resulted"
    assert urine.result == "Few pus cells"
    assert urine.notes == "Sample slightly haemolysed"
    assert urine.min_normal is None and urine.max_normal is None
    assert urine.abnormal is None

    [bg] = rows[ORDER_BG]
    assert bg.status == "resulted"
    assert bg.test_uuid == TEST_BG
    assert bg.result == "B Positive"
    assert bg.result_uuid == ANSWER_B_POS
    assert bg.abnormal is False


@respx.mock
async def test_pending_row_parsed_as_pending() -> None:
    rows = _rows_by_order(await _read_mixed())
    [creat] = rows[ORDER_CREAT]
    assert creat.status == "pending"
    assert creat.result is None
    assert creat.result_at is None
    assert creat.provider is None
    assert creat.referred_out is False


@respx.mock
async def test_panel_rows_carry_panel_uuid_and_name() -> None:
    rows = _rows_by_order(await _read_mixed())
    panel_rows = rows[ORDER_PANEL]
    assert len(panel_rows) == 2
    assert {r.panel_uuid for r in panel_rows} == {PANEL}
    assert {r.panel_name for r in panel_rows} == {"Test Liver Function Test"}
    assert [r.test_name for r in panel_rows] == ["Test SGOT", "Test SGPT"]
    assert all(r.status == "resulted" for r in panel_rows)
    assert panel_rows[1].abnormal is True
    assert panel_rows[1].max_normal == Decimal("56.0")


@respx.mock
async def test_referred_out_row() -> None:
    rows = _rows_by_order(await _read_mixed())
    [tsh] = rows[ORDER_TSH]
    assert tsh.referred_out is True
    assert tsh.result is None
    # A referred-out test is a result the lab filed (it has a result time), not pending.
    assert tsh.status == "resulted"
    assert tsh.notes == "Sent to reference lab"
    assert tsh.result_at == datetime(2026, 10, 20, 5, 10, tzinfo=timezone.utc)


@respx.mock
async def test_mixed_fixture_row_count_and_order() -> None:
    rows = await _read_mixed()
    assert [r.order_uuid for r in rows] == [
        ORDER_HB,
        ORDER_URINE,
        ORDER_BG,
        ORDER_PANEL,
        ORDER_PANEL,
        ORDER_CREAT,
        ORDER_TSH,
    ]


@respx.mock
async def test_missing_optional_keys_tolerated() -> None:
    _login()
    respx.get(LAB_RESULTS).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"orderUuid": ORDER_CREAT, "testUuid": TEST_HB},
                    {
                        "orderUuid": ORDER_HB,
                        "testUuid": TEST_HB,
                        "result": 12.5,
                        "minNormal": "not-a-number",
                        "maxNormal": 0.1,
                        "resultDateTime": 1792468800000,
                    },
                    # Unusable rows (no order or test uuid) are skipped, not fatal.
                    {"testUuid": TEST_HB, "result": "1"},
                    {"orderUuid": ORDER_URINE},
                    "garbage",
                ]
            },
        )
    )
    rows = await _client().read_lab_results(PATIENT)
    assert len(rows) == 2
    bare, numeric = rows
    assert bare.status == "pending"
    assert bare.test_name == ""
    assert bare.accession_uuid is None
    assert bare.unit is None
    assert bare.result is None
    assert bare.min_normal is None and bare.max_normal is None
    assert bare.abnormal is None
    assert bare.referred_out is False
    assert bare.uploaded_file_name is None
    assert numeric.status == "resulted"
    assert numeric.result == "12.5"
    assert numeric.min_normal is None
    # Decimal from the JSON number's text, not its binary float value.
    assert str(numeric.max_normal) == "0.1"
    assert numeric.result_at is not None and numeric.result_at.tzinfo is not None


@respx.mock
async def test_unexpected_body_is_vendor_rejected() -> None:
    _login()
    respx.get(LAB_RESULTS).mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(VendorRejected):
        await _client().read_lab_results(PATIENT)


@respx.mock
async def test_unknown_patient_404_returns_empty_list() -> None:
    _login()
    respx.get(LAB_RESULTS).mock(return_value=httpx.Response(404, text="<html>Not Found</html>"))
    assert await _client().read_lab_results(PATIENT) == []


@respx.mock
async def test_read_500_is_transient() -> None:
    _login()
    respx.get(LAB_RESULTS).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(TransientError):
        await _client().read_lab_results(PATIENT)


# --- test definitions -------------------------------------------------------


@respx.mock
async def test_read_test_definitions_coded_answers_and_units() -> None:
    _login()
    coded = respx.get(f"{CONCEPT}/{TEST_BG}").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": TEST_BG,
                "datatype": {"name": "Coded"},
                "answers": [
                    {"uuid": ANSWER_B_POS, "display": "B Positive"},
                    {"uuid": ANSWER_O_POS, "display": "O Positive"},
                ],
            },
        )
    )
    numeric = respx.get(f"{CONCEPT}/{TEST_HB}").mock(
        return_value=httpx.Response(
            200,
            json={"uuid": TEST_HB, "datatype": {"name": "Numeric"}, "units": "g/dL", "answers": []},
        )
    )
    defs = await _client().read_test_definitions([TEST_BG, TEST_HB, TEST_BG])
    assert defs == [
        LabTestDefinition(
            concept_uuid=TEST_BG,
            datatype="Coded",
            units=None,
            answers=((ANSWER_B_POS, "B Positive"), (ANSWER_O_POS, "O Positive")),
        ),
        LabTestDefinition(concept_uuid=TEST_HB, datatype="Numeric", units="g/dL", answers=()),
    ]
    # De-duplicated: one read per concept.
    assert coded.call_count == 1
    assert numeric.call_count == 1
    rep = coded.calls.last.request.url.params["v"]
    assert rep == "custom:(uuid,datatype:(name),units,answers:(uuid,display))"


@respx.mock
async def test_read_test_definitions_other_datatype_and_404() -> None:
    _login()
    respx.get(f"{CONCEPT}/{TEST_HB}").mock(
        return_value=httpx.Response(200, json={"uuid": TEST_HB, "datatype": {"name": "Document"}})
    )
    [d] = await _client().read_test_definitions([TEST_HB])
    assert d.datatype == "Other" and d.units is None and d.answers == ()

    respx.get(f"{CONCEPT}/{TEST_BG}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().read_test_definitions([TEST_BG])


@respx.mock
async def test_read_test_definitions_validates_uuids_before_io() -> None:
    route = respx.route()
    with pytest.raises(ValueError):
        await _client().read_test_definitions([TEST_HB, "../session"])
    assert route.call_count == 0
    assert await _client().read_test_definitions([]) == []
