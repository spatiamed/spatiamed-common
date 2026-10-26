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
    _result_encounter_body,
)
from sm_common.integrations.exceptions import (
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.lab_results_types import (
    LabResultRow,
    LabResultWrite,
    LabTestDefinition,
)

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


# --- result encounter body (pure, no I/O) -------------------------------------

ENCOUNTER_TYPE = "0c061ae4-61b8-47a5-ab53-c21bc95dbb68"
C_ABNORMAL = "8d542a8a-b673-4c57-8a7d-8ea4ff3235cb"
C_MINNORMAL = "bad1abd9-40ca-4334-bfe7-e092961aef46"
C_MAXNORMAL = "ec402d6a-7c24-4a7a-8af0-5dbd9d03b006"
C_NOTES = "901f8cac-33a7-44f2-b68d-ab7b7b90b734"
C_REFERRED_OUT = "19e49acf-8224-4bdf-ab57-d41caee5ebfd"
CFG_LOCATION = "4634dde0-2ee1-4e71-b59f-545c4954b00b"
CALLER_LOCATION = "9ff9b90f-291a-4beb-be6b-6504d9b45b9e"
PROVIDER = "f2e6c810-53eb-4c90-ae9d-03f5356e0e28"
TEST_SGOT = "d6193f4c-8ba0-48bb-a705-93f8289ffd4c"
TEST_SGPT = "e92d1c4f-aa52-465e-8764-68f3fab3e056"
TEST_URINE = "4ea2a4e3-742e-4c89-a272-58a4dd5a6cc6"
ENCOUNTER_AT = datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc)
RESULT_AT = datetime(2026, 10, 20, 9, 15, 0, 250000, tzinfo=timezone.utc)
RESULT_AT_LATER = datetime(2026, 10, 20, 9, 20, tzinfo=timezone.utc)

FULL_CFG = LabResultsConfig(
    lab_result_encounter_type_uuid=ENCOUNTER_TYPE,
    concept_lab_abnormal_uuid=C_ABNORMAL,
    concept_lab_minnormal_uuid=C_MINNORMAL,
    concept_lab_maxnormal_uuid=C_MAXNORMAL,
    concept_lab_notes_uuid=C_NOTES,
    concept_referred_out_uuid=C_REFERRED_OUT,
    result_location_uuid=CFG_LOCATION,
)


def _hb(**over: object) -> LabResultWrite:
    base: dict[str, object] = {
        "order_uuid": ORDER_HB,
        "test_concept_uuid": TEST_HB,
        "panel_concept_uuid": None,
        "value_kind": "numeric",
        "value": Decimal("11.2"),
        "min_normal": Decimal("13.0"),
        "max_normal": Decimal("17.0"),
        "abnormal": True,
        "notes": "Synthetic note: repeat in two weeks",
        "result_at": RESULT_AT,
    }
    base.update(over)
    return LabResultWrite(**base)  # type: ignore[arg-type]


def _panel_results() -> list[LabResultWrite]:
    return [
        LabResultWrite(
            order_uuid=ORDER_PANEL,
            test_concept_uuid=TEST_SGOT,
            panel_concept_uuid=PANEL,
            value_kind="numeric",
            value=Decimal("52"),
            min_normal=Decimal("5"),
            max_normal=Decimal("40"),
            abnormal=True,
            result_at=RESULT_AT,
        ),
        LabResultWrite(
            order_uuid=ORDER_PANEL,
            test_concept_uuid=TEST_SGPT,
            panel_concept_uuid=PANEL,
            value_kind="text",
            value="Haemolysed sample",
            min_normal=Decimal("5"),
            max_normal=Decimal("40"),
            referred_out=True,
            result_at=RESULT_AT_LATER,
        ),
    ]


def _body(
    results: list[LabResultWrite],
    *,
    cfg: LabResultsConfig = FULL_CFG,
    location_uuid: str = "",
    encounter_datetime: datetime = ENCOUNTER_AT,
) -> dict:
    return _result_encounter_body(
        cfg,
        patient_uuid=PATIENT,
        visit_uuid=VISIT_A,
        provider_uuid=PROVIDER,
        location_uuid=location_uuid,
        encounter_datetime=encounter_datetime,
        results=results,
    )


def _walk(obs_list: list[dict]) -> list[dict]:
    out: list[dict] = []
    for obs in obs_list:
        out.append(obs)
        out.extend(_walk(obs.get("groupMembers", [])))
    return out


def _leaves(body: dict) -> list[dict]:
    return [o for o in _walk(body["observations"]) if "groupMembers" not in o]


def _leaf_concepts(body: dict) -> list[str]:
    return [o["concept"]["uuid"] for o in _leaves(body)]


def test_single_numeric_body_matches_fixture() -> None:
    body = _body([_hb()])
    # Round-trip through JSON: a Decimal left in the body would fail to serialise.
    assert json.loads(json.dumps(body)) == _fixture("expected_body_single_numeric.json")
    [value_leaf] = [o for o in _leaves(body) if o["concept"]["uuid"] == TEST_HB]
    assert isinstance(value_leaf["value"], float)
    assert f"{value_leaf['value']:.6g}" == f"{Decimal('11.2'):.6g}"


def test_panel_body_matches_fixture() -> None:
    body = _body(_panel_results())
    assert json.loads(json.dumps(body)) == _fixture("expected_body_panel.json")


def test_caller_location_wins_over_config_location() -> None:
    assert _body([_hb()], location_uuid=CALLER_LOCATION)["locationUuid"] == CALLER_LOCATION
    assert _body([_hb()])["locationUuid"] == CFG_LOCATION


def test_every_obs_level_carries_order_uuid() -> None:
    other_order = ORDER_URINE
    results = [
        _hb(),
        _panel_results()[0],
        _panel_results()[1],
        LabResultWrite(
            order_uuid=other_order,
            test_concept_uuid=TEST_URINE,
            panel_concept_uuid=None,
            value_kind="text",
            value="Nil",
            notes="Synthetic",
            referred_out=True,
            result_at=RESULT_AT,
        ),
    ]
    body = _body(results)
    every = _walk(body["observations"])
    assert len(every) > len(body["observations"])
    for obs in every:
        assert obs.get("orderUuid") in (ORDER_HB, ORDER_PANEL, other_order), obs
        assert isinstance(obs["observationDateTime"], int)
    # Each top-level group carries its own order down every level.
    for top in body["observations"]:
        assert {o["orderUuid"] for o in _walk([top])} == {top["orderUuid"]}


def test_panel_uses_panel_order_on_all_levels() -> None:
    body = _body(_panel_results())
    [panel] = body["observations"]
    assert panel["concept"]["uuid"] == PANEL
    tests = panel["groupMembers"]
    assert [t["concept"]["uuid"] for t in tests] == [TEST_SGOT, TEST_SGPT]
    for test in tests:
        [inner] = test["groupMembers"]
        assert inner["concept"]["uuid"] == test["concept"]["uuid"]
        assert "value" not in test and "value" not in inner
        for leaf in inner["groupMembers"]:
            assert "groupMembers" not in leaf
    assert {o["orderUuid"] for o in _walk(body["observations"])} == {ORDER_PANEL}
    # The panel node carries the latest member result time.
    assert panel["observationDateTime"] == 1792488000000


def test_separate_orders_are_separate_top_level_groups() -> None:
    body = _body([_hb(), *_panel_results()])
    assert [o["orderUuid"] for o in body["observations"]] == [ORDER_HB, ORDER_PANEL]


def test_ranges_written_only_for_numeric_with_both_bounds() -> None:
    ranges = {C_MINNORMAL, C_MAXNORMAL}
    text_with_bounds = _hb(value_kind="text", value="Clotted", abnormal=False, notes=None)
    assert not ranges & set(_leaf_concepts(_body([text_with_bounds])))

    coded_with_bounds = _hb(value_kind="coded", value=ANSWER_B_POS, abnormal=False, notes=None)
    assert not ranges & set(_leaf_concepts(_body([coded_with_bounds])))

    only_min = _hb(max_normal=None, abnormal=False, notes=None)
    assert not ranges & set(_leaf_concepts(_body([only_min])))
    only_max = _hb(min_normal=None, abnormal=False, notes=None)
    assert not ranges & set(_leaf_concepts(_body([only_max])))

    both = _body([_hb(abnormal=False, notes=None)])
    assert _leaf_concepts(both) == [TEST_HB, C_MINNORMAL, C_MAXNORMAL]
    # No range concept keys needed when no range is written.
    no_range_cfg = LabResultsConfig(
        lab_result_encounter_type_uuid=ENCOUNTER_TYPE, result_location_uuid=CFG_LOCATION
    )
    assert _leaf_concepts(_body([only_min], cfg=no_range_cfg)) == [TEST_HB]


def test_abnormal_written_only_when_true() -> None:
    assert C_ABNORMAL not in _leaf_concepts(_body([_hb(abnormal=False)]))
    leaves = {o["concept"]["uuid"]: o for o in _leaves(_body([_hb(abnormal=True)]))}
    assert leaves[C_ABNORMAL]["value"] is True


def test_coded_value_is_uuid_object() -> None:
    body = _body([_hb(value_kind="coded", value=ANSWER_B_POS, abnormal=False, notes=None)])
    [leaf] = _leaves(body)
    assert leaf["value"] == {"uuid": ANSWER_B_POS}


def test_text_value_is_string() -> None:
    body = _body([_hb(value_kind="text", value="Nil seen", abnormal=False, notes=None)])
    [leaf] = _leaves(body)
    assert leaf["value"] == "Nil seen"


def test_referred_out_and_notes_members() -> None:
    body = _body([_hb(abnormal=False, min_normal=None, notes="Sent out", referred_out=True)])
    leaves = _leaves(body)
    assert [o["concept"]["uuid"] for o in leaves] == [TEST_HB, C_NOTES, C_REFERRED_OUT]
    assert leaves[1]["value"] == "Sent out"
    assert leaves[2]["value"] is True
    # Blank notes and referred_out=False write nothing.
    quiet = _body([_hb(abnormal=False, min_normal=None, notes="  ", referred_out=False)])
    assert _leaf_concepts(quiet) == [TEST_HB]


def test_naive_datetime_is_value_error() -> None:
    with pytest.raises(ValueError):
        _body([_hb(result_at=datetime(2026, 10, 20, 9, 15))])
    with pytest.raises(ValueError):
        _body([_hb()], encounter_datetime=datetime(2026, 10, 20, 9, 30))


def test_write_without_location_is_config_error_before_io() -> None:
    cfg = LabResultsConfig(
        lab_result_encounter_type_uuid=ENCOUNTER_TYPE,
        concept_lab_abnormal_uuid=C_ABNORMAL,
        concept_lab_minnormal_uuid=C_MINNORMAL,
        concept_lab_maxnormal_uuid=C_MAXNORMAL,
        concept_lab_notes_uuid=C_NOTES,
        concept_referred_out_uuid=C_REFERRED_OUT,
    )
    with respx.mock(assert_all_called=False) as router:
        route = router.route()
        with pytest.raises(ConfigError, match="result_location_uuid"):
            _body([_hb()], cfg=cfg, location_uuid="")
        assert route.call_count == 0


def test_missing_encounter_type_is_config_error_naming_key() -> None:
    cfg = LabResultsConfig(**{**FULL_CFG.__dict__, "lab_result_encounter_type_uuid": ""})
    with pytest.raises(ConfigError, match="lab_result_encounter_type_uuid"):
        _body([_hb()], cfg=cfg)


@pytest.mark.parametrize(
    ("key", "result"),
    [
        ("concept_lab_abnormal_uuid", {"abnormal": True}),
        ("concept_lab_minnormal_uuid", {"abnormal": False}),
        ("concept_lab_maxnormal_uuid", {"abnormal": False}),
        ("concept_lab_notes_uuid", {"abnormal": False, "min_normal": None}),
        (
            "concept_referred_out_uuid",
            {"abnormal": False, "min_normal": None, "notes": None, "referred_out": True},
        ),
    ],
)
def test_missing_concept_key_is_config_error_naming_key(
    key: str, result: dict[str, object]
) -> None:
    cfg = LabResultsConfig(**{**FULL_CFG.__dict__, key: ""})
    with pytest.raises(ConfigError, match=key):
        _body([_hb(**result)], cfg=cfg)


@pytest.mark.parametrize(
    "bad",
    [
        {"order_uuid": "../obs"},
        {"test_concept_uuid": ""},
        {"panel_concept_uuid": "not a uuid"},
        {"value_kind": "numeric", "value": "11.2"},
        {"value_kind": "numeric", "value": Decimal("NaN")},
        {"value_kind": "text", "value": Decimal("1")},
        {"value_kind": "text", "value": "   "},
        {"value_kind": "coded", "value": "B Positive"},
        {"value_kind": "image", "value": "x"},
        {"min_normal": 13.0},
    ],
)
def test_invalid_result_is_value_error(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _body([_hb(**bad)])


def test_empty_results_and_bad_ids_are_value_errors() -> None:
    with pytest.raises(ValueError):
        _body([])
    with pytest.raises(ValueError):
        _result_encounter_body(
            FULL_CFG,
            patient_uuid="",
            visit_uuid=VISIT_A,
            provider_uuid=PROVIDER,
            location_uuid="",
            encounter_datetime=ENCOUNTER_AT,
            results=[_hb()],
        )


def test_conflicting_order_grouping_is_value_error() -> None:
    # One single-test order written twice would leave two result groups for it.
    with pytest.raises(ValueError):
        _body([_hb(), _hb(value=Decimal("12.0"))])
    # The same test twice within one panel order.
    first = _panel_results()[0]
    with pytest.raises(ValueError):
        _body([first, first])
    # One order used both as a single test and as a panel.
    with pytest.raises(ValueError):
        _body([_hb(), _hb(test_concept_uuid=TEST_SGOT, panel_concept_uuid=PANEL)])
