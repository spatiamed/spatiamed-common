"""Radiology live-harness checks, run offline against a fake Bahmni.

The fake answers only the routes the radiology client and the checks use; patient
creation and the visit start are stubbed on the record client. Synthetic data only.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import date
from typing import Any

import httpx
import pytest

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.record_types import RecordAddress, RecordPatient, RecordVisit

HARNESS = pathlib.Path(__file__).resolve().parents[2] / "harness" / "bahmni"
sys.path.insert(0, str(HARNESS))
import adt_checks  # noqa: E402
import phase4_radiology  # noqa: E402

REST = "/openmrs/ws/rest/v1"
ORDER_TYPE = "7e3a1c55-2b9d-4f0e-8a61-0c4d2e9b7f13"
ORDERABLE_SET = "c1f2a3b4-5d6e-4f70-8a9b-0c1d2e3f4a5b"
REPORT_CONCEPT = "5a7b3c21-4d8e-4f90-a1b2-c3d4e5f60718"
REPORT_ENC_TYPE = "6b8c4d32-5e9f-4a01-b2c3-d4e5f6071829"
DOC_ENC_TYPE = "9d1e2f30-4a5b-4c6d-8e7f-a0b1c2d3e4f5"
PROC = "b0fb2bb8-5f71-4811-8c87-9616d791dc3f"
ORDER = "dbdd61b2-6a72-4cb1-aa5b-7a03e0f46ea9"
OBS = "0d6f1c2e-91a3-4f7b-8e52-3c1a9b7d6e40"
OTHER_OBS = "7c2d9e41-3b5a-4c8f-9e10-2a4b6c8d0e1f"
OTHER_VISIT = "a3c5e7f9-1b2d-4e6f-8a0c-2e4f6a8c0e2a"
DOC_ENC = "2f4e6a8c-1b3d-4f5a-9c7e-0a2b4c6d8e1f"
PATIENT = "3e6b15ff-0e45-476a-967a-fb78f2aee234"
VISIT = "bcdf607f-e497-468f-959c-02715c1d9760"
PROVIDER = "4c5d6e7f-8091-4a2b-b3c4-d5e6f7a8b9c0"
LOCATION = "8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c2d"
CONSULT_TYPE = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
OPD_VISIT_TYPE = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9"
RADIOLOGY = {
    "radiology_order_type_uuid": ORDER_TYPE,
    "orderable_set_uuid": ORDERABLE_SET,
    "report_concept_uuid": REPORT_CONCEPT,
    "report_encounter_type_uuid": REPORT_ENC_TYPE,
    "document_encounter_type_uuid": DOC_ENC_TYPE,
    "viewer_go_live": None,
}


def _fake(state: dict[str, Any]) -> httpx.MockTransport:
    """A Bahmni that knows one mapped procedure, one order, one report and one document."""

    def order_row() -> dict[str, Any]:
        return {
            "uuid": ORDER,
            "orderNumber": state["order_number"],
            "action": "NEW",
            "dateActivated": "2026-10-26T10:00:00.000+0000",
            "dateStopped": "2026-10-26T10:05:00.000+0000" if state["stopped"] else None,
            "autoExpireDate": None,
            "orderType": {"uuid": ORDER_TYPE},
            "careSetting": {"careSettingType": "OUTPATIENT"},
            "concept": {"uuid": PROC},
        }

    async def handle(req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        state["calls"].append((method, path))
        if path.endswith("/session"):
            return httpx.Response(
                200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=x"}
            )
        body = json.loads(req.content) if req.content else None
        if method == "GET" and path == f"{REST}/ordertype/{ORDER_TYPE}":
            return httpx.Response(200, json={"uuid": ORDER_TYPE, "name": state["order_type_name"]})
        if method == "GET" and path == f"{REST}/concept/{ORDERABLE_SET}":
            source = "PACS Procedure Code" if state["mapped"] else "Other Source"
            return httpx.Response(
                200,
                json={
                    "setMembers": [
                        {
                            "uuid": PROC,
                            "display": "LT-P4-XRAY-CHEST",
                            "mappings": [
                                {
                                    "conceptReferenceTerm": {
                                        "code": "XR-CH",
                                        "conceptSource": {"name": source},
                                    }
                                }
                            ],
                        }
                    ]
                },
            )
        if method == "POST" and path == f"{REST}/bahmnicore/bahmniencounter":
            reply: dict[str, Any] = {"encounterUuid": body["encounterUuid"], "orders": []}
            for o in body.get("orders") or []:
                if o.get("action") == "DISCONTINUE":
                    if state["discontinue_fails"]:
                        return httpx.Response(500, json={"error": {}})
                    state["events"].append("discontinue")
                    state["discontinued"].append(o)
                    state["stopped"] = True
                else:
                    state["placed"] = True
                    reply["orders"].append({"uuid": ORDER, "concept": {"uuid": PROC}})
            if body.get("observations"):
                state["report_bodies"].append(body["observations"])
                obs = body["observations"][0]
                reply["observations"] = [
                    {
                        "uuid": OBS,
                        "concept": {"uuid": obs["concept"]["uuid"]},
                        "orderUuid": obs.get("orderUuid"),
                        "voided": False,
                    }
                ]
            return httpx.Response(200, json=reply)
        if method == "GET" and path == f"{REST}/order/{ORDER}":
            if state["readback_fails"] > 0:
                state["readback_fails"] -= 1
                return httpx.Response(500, json={"error": {}})
            row = order_row()
            # The harness's own wait-then-read asks a narrower rep than the client.
            if state["stopped_at_check"] and "action" not in req.url.params.get("v", ""):
                row["dateStopped"] = "2026-10-26T10:05:00.000+0000"
            return httpx.Response(200, json=row)
        if method == "GET" and path == f"{REST}/visit/{VISIT}":
            orders = [order_row()] if state["placed"] and state["visit_lists_order"] else []
            return httpx.Response(200, json={"encounters": [{"orders": orders}]})
        if method == "GET" and path == f"{REST}/pacs/studies":
            state["pacs_params"] = dict(req.url.params)
            if state["pacs_status"] != 200:
                return httpx.Response(state["pacs_status"], json={"error": {}})
            return httpx.Response(200, json=[])
        if method == "GET" and path == f"{REST}/obs":
            state["obs_params"] = dict(req.url.params)
            enc = {
                "uuid": "e1",
                "encounterType": {"uuid": REPORT_ENC_TYPE, "name": state["report_enc_name"]},
            }
            results = [{"uuid": OBS, "voided": False, "order": {"uuid": ORDER}, "encounter": enc}]
            if state["second_obs"]:
                results.append(
                    {"uuid": OTHER_OBS, "voided": False, "order": {"uuid": ORDER}, "encounter": enc}
                )
            return httpx.Response(200, json={"results": results})
        if method == "DELETE" and path == f"{REST}/obs/{OBS}":
            state["obs_voids"] += 1
            state["obs_voided"] = state["void_sticks"]
            return httpx.Response(204)
        if method == "GET" and path == f"{REST}/obs/{OBS}":
            return httpx.Response(200, json={"uuid": OBS, "voided": state["obs_voided"]})
        if method == "POST" and path == f"{REST}/bahmnicore/visitDocument/uploadDocument":
            state["upload"] = body
            return httpx.Response(200, json={"url": "1001/1001-RADIOLOGY-x__LT-P4-film.pdf"})
        if method == "POST" and path == f"{REST}/bahmnicore/visitDocument":
            state["filing"] = body
            return httpx.Response(200, json={"encounterUuid": DOC_ENC, "visitUuid": VISIT})
        if method == "GET" and path == f"{REST}/encounter/{DOC_ENC}":
            return httpx.Response(
                200,
                json={
                    "uuid": DOC_ENC,
                    "visit": {"uuid": state["doc_visit"]},
                    "encounterType": {"uuid": state["doc_enc_type"], "name": "RADIOLOGY"},
                },
            )
        if method == "DELETE" and path == f"{REST}/encounter/{DOC_ENC}":
            state["doc_voided"] = True
            return httpx.Response(204)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls": [],
        "mapped": True,
        "order_number": "ORD-1234",
        "placed": False,
        "stopped": False,
        "discontinued": [],
        "readback_fails": 0,
        "events": [],
        "pacs_status": 200,
        "report_bodies": [],
        "obs_voids": 0,
        "obs_voided": False,
        "doc_voided": False,
        "patients": [],
        "dropped": [],
        "order_type_name": "Radiology Order",
        "visit_lists_order": True,
        "stopped_at_check": False,
        "second_obs": False,
        "report_enc_name": "RADIOLOGY",
        "void_sticks": True,
        "doc_visit": VISIT,
        "doc_enc_type": DOC_ENC_TYPE,
        "discontinue_fails": False,
    }
    state.update(over)
    return state


def _ctx(state: dict[str, Any], tmp_path, monkeypatch) -> adt_checks.AdtCtx:
    monkeypatch.setenv("HARNESS_SAMPLES_DIR", str(tmp_path))
    monkeypatch.setattr(phase4_radiology, "IMAGING_WAIT_S", 0)
    http = BahmniHttp("https://b.test", "u", "p", transport=_fake(state))
    admin = BahmniHttp("https://b.test", "a", "p", transport=_fake(state))
    config = BahmniConfig(
        location_uuid=LOCATION,
        identifier_type_uuid="t",
        identifier_source_uuid="s",
        opd_visit_type_uuid=OPD_VISIT_TYPE,
        consultation_encounter_type_uuid=CONSULT_TYPE,
    )
    x = adt_checks.AdtCtx(
        {"harness": {"provider_uuid": PROVIDER, "radiology": RADIOLOGY}}, http, config, admin
    )

    async def create_patient(p: Any) -> tuple[RecordPatient, bool]:
        state["patients"].append(p)
        return (
            RecordPatient(
                uuid=PATIENT,
                identifier="LTP4000001",
                given=p.given,
                family=p.family,
                gender="F",
                birth_date=date(1985, 1, 1),
                birth_date_estimated=False,
                age_years=None,
                phone=None,
                address=RecordAddress(),
                dead=False,
            ),
            True,
        )

    async def start_visit(patient: str, visit_type: str | None = None) -> tuple[RecordVisit, bool]:
        from datetime import UTC, datetime

        return (
            RecordVisit(
                uuid=VISIT,
                visit_type="OPD",
                start=datetime.now(UTC),
                stop=None,
                location_uuid=LOCATION,
            ),
            True,
        )

    async def drop_patient(uuid: str) -> str | None:
        state["events"].append("drop")
        state["dropped"].append(uuid)
        return None

    monkeypatch.setattr(x.record, "create_patient", create_patient)
    monkeypatch.setattr(x.record, "start_visit", start_visit)
    monkeypatch.setattr(x, "_drop_patient", drop_patient)
    return x


async def test_run_checks_returns_one_row_per_check(tmp_path, monkeypatch):
    state = _state()
    x = _ctx(state, tmp_path, monkeypatch)
    rows = await phase4_radiology.run_checks(x)
    names = [n for n, _topic, _r in rows]
    assert names == [n for n, _topic, _fn in phase4_radiology.CHECKS]
    assert len(set(names)) == len(names) == 7
    statuses = {n: r[0] for n, _topic, r in rows}
    assert set(statuses.values()) == {"PASS"}, rows
    # The report read-back searched by patient + report concept and found the order link.
    assert state["obs_params"]["concept"] == REPORT_CONCEPT
    assert state["report_bodies"][0][0]["orderUuid"] == ORDER
    # The study query went out with the patient identifier, through the record only.
    assert state["pacs_params"] == {"patientId": "LTP4000001"}
    assert all(p.startswith("/openmrs/") for _m, p in state["calls"])
    # The filing went under the configured document encounter type into the visit.
    assert state["filing"]["encounterTypeUuid"] == DOC_ENC_TYPE
    assert state["filing"]["visitUuid"] == VISIT
    assert state["upload"]["content"]


async def test_cleanup_discontinues_orders(tmp_path, monkeypatch):
    state = _state()
    x = _ctx(state, tmp_path, monkeypatch)
    await phase4_radiology.run_checks(x)
    assert len(state["discontinued"]) == 1
    dc = state["discontinued"][0]
    assert dc["previousOrderUuid"] == ORDER and dc["action"] == "DISCONTINUE"
    assert state["obs_voided"] and state["obs_voids"] == 1, "report voided once, not twice"
    assert state["doc_voided"], "the filed document encounter is voided"
    assert state["dropped"] == [PATIENT]
    assert state["events"] == ["discontinue", "drop"], "orders stop before the patient is voided"
    assert x.cleanups == [], "run_checks leaves no cleanup for a later runner to repeat"


async def test_cleanup_discontinues_an_order_whose_read_back_failed(tmp_path, monkeypatch):
    # The order landed but its read-back 500'd: the check fails, and cleanup still
    # finds the order on the visit and discontinues it.
    state = _state(readback_fails=1)
    x = _ctx(state, tmp_path, monkeypatch)
    rows = await phase4_radiology.run_checks(x)
    statuses = {n: r[0] for n, _t, r in rows}
    assert statuses["radiology_order_placed"] == "FAIL"
    assert state["placed"]
    assert [d["previousOrderUuid"] for d in state["discontinued"]] == [ORDER]
    assert state["events"][-1] == "drop"


async def test_order_number_over_16_fails_and_is_discontinued(tmp_path, monkeypatch):
    state = _state(order_number="ORD-12345678901234")
    x = _ctx(state, tmp_path, monkeypatch)
    rows = await phase4_radiology.run_checks(x)
    statuses = {n: r for n, _t, r in rows}
    status, detail = statuses["radiology_order_placed"]
    assert status == "FAIL" and "order_number_too_long" in detail
    assert [d["previousOrderUuid"] for d in state["discontinued"]] == [ORDER]


async def test_pacs_501_recorded_as_fail_with_gp_name(tmp_path, monkeypatch):
    state = _state(pacs_status=501)
    x = _ctx(state, tmp_path, monkeypatch)
    rows = await phase4_radiology.run_checks(x)
    status, detail = {n: r for n, _t, r in rows}["pacs_studies_endpoint"]
    assert status == "FAIL"
    assert "pacs_not_configured" in detail and "pacsquery.pacsConfig" in detail


async def test_unmapped_procedures_fail_resolution_and_block_orders(tmp_path, monkeypatch):
    state = _state(mapped=False)
    x = _ctx(state, tmp_path, monkeypatch)
    rows = {n: r for n, _t, r in await phase4_radiology.run_checks(x)}
    assert rows["radiology_order_type_resolved"][0] == "FAIL"
    assert "mapped=0" in rows["radiology_order_type_resolved"][1]
    assert rows["radiology_order_placed"][0] == "INCONCLUSIVE"
    assert not state["placed"], "no order is placed without a mapped procedure"


async def test_patients_are_lt_p4_named(tmp_path, monkeypatch):
    state = _state()
    x = _ctx(state, tmp_path, monkeypatch)
    await phase4_radiology.run_checks(x)
    assert len(state["patients"]) == 1, "one synthetic patient per run"
    p = state["patients"][0]
    assert p.given.startswith("LT-P4-") and p.family.startswith("LT-P4-")


def test_imaging_wait_is_at_least_30_seconds():
    assert phase4_radiology.IMAGING_WAIT_S >= 30


def test_no_host_or_env_literals():
    src = (HARNESS / "phase4_radiology.py").read_text()
    for bad in ("os.environ", "getenv", "import os", "http://", "https://", "localhost"):
        assert bad not in src, f"phase4_radiology.py contains {bad!r}"
    assert not re.search(r"\bLT-P2-", src), "radiology checks use LT-P4- names only"
    assert not re.search(r"\bP\d-L[V]-|\bL[V]-\d", src), "no internal check labels"
    hits = adt_checks.UUID_TOKEN.findall(src)
    assert not hits, f"uuid literals: {hits[:3]}"
    # The imaging system is reached only through the record's REST surface.
    for bad in ("dcm4chee", "oviyam", ":8055", ":11112"):
        assert bad not in src.lower()


@pytest.mark.parametrize("name", [n for n, _t, _f in phase4_radiology.CHECKS])
def test_check_topics_are_slugs(name):
    topic = {n: t for n, t, _f in phase4_radiology.CHECKS}[name]
    assert re.fullmatch(r"[a-z0-9_]+", topic)


# ─── each PASS condition also FAILs: one fake variant per guard ─────────────


async def _rows(state: dict[str, Any], tmp_path, monkeypatch) -> dict[str, Any]:
    x = _ctx(state, tmp_path, monkeypatch)
    return {n: r for n, _t, r in await phase4_radiology.run_checks(x)}


async def test_order_missing_from_visit_fails_placement(tmp_path, monkeypatch):
    rows = await _rows(_state(visit_lists_order=False), tmp_path, monkeypatch)
    status, detail = rows["radiology_order_placed"]
    assert status == "FAIL" and "found_by_visit=False" in detail


async def test_order_type_with_another_name_fails_resolution(tmp_path, monkeypatch):
    rows = await _rows(_state(order_type_name="Lab Order"), tmp_path, monkeypatch)
    status, detail = rows["radiology_order_type_resolved"]
    assert status == "FAIL" and "'Lab Order'" in detail


async def test_order_stopped_before_imaging_wait_ends_fails(tmp_path, monkeypatch):
    rows = await _rows(_state(stopped_at_check=True), tmp_path, monkeypatch)
    status, detail = rows["radiology_order_reaches_imaging"]
    assert status == "FAIL" and "not active" in detail


async def test_second_unvoided_report_on_the_order_fails_readback(tmp_path, monkeypatch):
    rows = await _rows(_state(second_obs=True), tmp_path, monkeypatch)
    status, detail = rows["radiology_report_readback"]
    assert status == "FAIL" and "obs_by_order=2" in detail


async def test_report_still_unvoided_fails_void(tmp_path, monkeypatch):
    rows = await _rows(_state(void_sticks=False), tmp_path, monkeypatch)
    status, detail = rows["radiology_report_void"]
    assert status == "FAIL" and "voided=False" in detail


@pytest.mark.parametrize(
    "over, shown",
    [
        ({"doc_enc_type": REPORT_ENC_TYPE}, "is_configured_type=False"),
        ({"doc_visit": OTHER_VISIT}, "same_visit=False"),
    ],
)
async def test_document_in_wrong_type_or_visit_fails(tmp_path, monkeypatch, over, shown):
    rows = await _rows(_state(**over), tmp_path, monkeypatch)
    status, detail = rows["radiology_document_filed"]
    assert status == "FAIL" and shown in detail


async def test_uuid_in_a_detail_line_is_scrubbed(tmp_path, monkeypatch):
    # Server text that carries a uuid (here an encounter type named by one) must not
    # reach a public detail line.
    rows = await _rows(_state(report_enc_name=OTHER_OBS), tmp_path, monkeypatch)
    status, detail = rows["radiology_report_readback"]
    assert status == "PASS"
    assert OTHER_OBS not in detail and "<uuid>" in detail


async def test_failed_discontinue_reports_cleanup_incomplete(tmp_path, monkeypatch, capsys):
    state = _state(discontinue_fails=True)
    x = _ctx(state, tmp_path, monkeypatch)
    await phase4_radiology.run_checks(x)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("CLEANUP radiology orders"))
    assert "FAILED" in line and "still active" in line, line
    assert "CLEANUP INCOMPLETE" in out
    assert x.state["rad_cleanup"][0] < x.state["rad_cleanup"][1]


async def test_empty_radiology_config_fails_resolution_and_cleans_up(tmp_path, monkeypatch, capsys):
    state = _state()
    x = _ctx(state, tmp_path, monkeypatch)
    x.h["radiology"] = {}
    rows = {n: r for n, _t, r in await phase4_radiology.run_checks(x)}
    status, detail = rows["radiology_order_type_resolved"]
    assert status == "FAIL" and detail.startswith("radiology config missing ")
    assert "radiology_order_type_uuid" in detail
    # The study query needs no radiology config; every other check is blocked or fails.
    assert all(r[0] != "PASS" for n, r in rows.items() if n != "pacs_studies_endpoint"), rows
    assert not state["placed"], "nothing is ordered without a config"
    assert state["dropped"] == [PATIENT], "a patient created on the way is still voided"
    # No order can exist without a config: the order cleanup is skipped, not FAILED.
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("CLEANUP radiology orders"))
    assert "SKIPPED" in line and "FAILED" not in line, line
    assert "CLEANUP INCOMPLETE" not in out
