"""Inpatient care live-harness checks, run offline against a fake Bahmni.

The fake answers the routes the inpatient care client and the checks use (ward
layout, ward patients, encounters, obs, concepts, the medication chart and its
administrations, user create). Patient, ward, bed and admission writes are stubbed on
the record client and drive the same fake state, so a bed's uuid churns when a patient
leaves it, as it does on Bahmni. Synthetic data only.
"""

from __future__ import annotations

import base64
import json
import math
import pathlib
import re
import sys
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.record_types import (
    RecordAddress,
    RecordAdmitResult,
    RecordBed,
    RecordBedAssignment,
    RecordEncounterResult,
    RecordPatient,
    RecordWard,
)

HARNESS = pathlib.Path(__file__).resolve().parents[2] / "harness" / "bahmni"
sys.path.insert(0, str(HARNESS))
import adt_checks  # noqa: E402
import phase4_ipd  # noqa: E402

REST = "/openmrs/ws/rest/v1"
WARD = "5d0c6f2a-8e41-4b37-9a65-1f2e3d4c5b6a"
BED_TYPE = "6e1d7a3b-9f52-4c48-8b76-2a3f4e5d6c7b"
PROVIDER = "4c5d6e7f-8091-4a2b-b3c4-d5e6f7a8b9c0"
LOCATION = "8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c2d"
VISIT_LOCATION = "9b0c1d2e-3f4a-4b5c-9d7e-8f9a0b1c2d3e"
CONSULT_TYPE = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
DRUG_OTHER = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e"
NURSING_TYPE = "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f"
NOTE_CONCEPT = "4d5e6f7a-8b9c-4d0e-9f2a-3b4c5d6e7f8a"
INTAKE_SET = "5e6f7a8b-9c0d-4e1f-8a3b-4c5d6e7f8a9b"
OUTPUT_SET = "6f7a8b9c-0d1e-4f2a-9b4c-5d6e7f8a9b0c"
PULSE = "7a8b9c0d-1e2f-4a3b-8c5d-6e7f8a9b0c1d"
SYSTOLIC = "8b9c0d1e-2f3a-4b4c-9d6e-7f8a9b0c1d2e"
DIASTOLIC = "9c0d1e2f-3a4b-4c5d-8e7f-8a9b0c1d2e3f"
NURSING = {
    "vitals_concepts": {"pulse": PULSE, "bp_systolic": SYSTOLIC, "bp_diastolic": DIASTOLIC},
    "intake_concept_set_uuid": INTAKE_SET,
    "output_concept_set_uuid": OUTPUT_SET,
    "nursing_note_concept_uuid": NOTE_CONCEPT,
    "nursing_encounter_type_uuid": NURSING_TYPE,
    # Keys the client does not use are ignored.
    "shift_starts": ["08:00", "20:00"],
}
SERVICE_ANSWER = {
    "MEDICATION_REQUEST": "MedicationRequest",
    "AS_NEEDED_PLACEHOLDER": "AsNeededPlaceholder",
    "AS_NEEDED_MEDICATION_REQUEST": "AsNeededMedicationRequest",
}
ADMIN_SLOT_STATUS = {"completed": "COMPLETED", "not-done": "NOT_DONE", "stopped": "STOPPED"}
PRIVILEGE_403 = {"error": {"message": "Privileges required: Get Medication Tasks"}}


def _u() -> str:
    return str(uuid4())


def _member(datatype: str) -> dict[str, Any]:
    return {"uuid": _u(), "display": f"LT-P4 {datatype}", "datatype": {"display": datatype}}


def _state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls": [],
        "beds": {},  # bed_id -> bed
        "next_bed_id": 101,
        "bed_types": [],
        "bed_type_deleted": False,
        "ward_deleted": False,
        "bed_deletes": [],
        "patients": {},  # uuid -> given name
        "visits": {},  # patient -> visit
        "dropped": [],
        "encounters": {},  # uuid -> encounter
        "orders": {},  # uuid -> order
        "slots": [],
        "schedule_posts": 0,
        "adhoc_posts": 0,
        "users": [],
        "user_deleted": False,
        "person_deleted": False,
        "io_members": [_member("Text"), _member("Numeric"), _member("Datetime")],
        # variants
        "uuid_churn": True,
        "cap_slots": True,
        "seconds_as_ms": False,
        "schedule_403": False,
        "null_dose_units": False,
        "adhoc_drops_notes": False,
        "unknown_encounter_status": 404,
        "io_refuses_datetime": False,
        "order_push_403": False,
    }
    state.update(over)
    return state


def _layout(state: dict[str, Any]) -> dict[str, Any]:
    cells = []
    for col in (1, 2, 3):
        bed = next(
            (b for b in state["beds"].values() if b["column"] == col and not b["deleted"]), None
        )
        cell: dict[str, Any] = {"rowNumber": 1, "columnNumber": col, "bedId": None}
        if bed is not None:
            cell.update(
                bedId=bed["id"],
                bedUuid=bed["uuid"],
                bedNumber=bed["number"],
                status="OCCUPIED" if bed["patients"] else "AVAILABLE",
                bedType={"name": bed["type"]} if bed["type"] else None,
                patients=[{"uuid": p} for p in bed["patients"]],
                bedTagMaps=[],
            )
        cells.append(cell)
    beds = [b for b in state["beds"].values() if not b["deleted"]]
    return {
        "ward": {"uuid": WARD, "display": "LT-P4 Ward"},
        "totalBeds": len(beds),
        "occupiedBeds": sum(1 for b in beds if b["patients"]),
        "bedLayouts": cells,
    }


def _free(state: dict[str, Any], patient: str) -> None:
    """A patient leaves its bed: bed management gives the bed a new uuid, clears its type."""
    for bed in state["beds"].values():
        if patient in bed["patients"]:
            bed["patients"].remove(patient)
            if state["uuid_churn"]:
                bed["uuid"] = _u()
                bed["type"] = None


def _slot(state: dict[str, Any], order: str, service: str, start: int, **kw: Any) -> dict[str, Any]:
    slot = {
        "uuid": _u(),
        "order": {"uuid": order, "dateStopped": None, "action": "NEW"},
        "serviceType": service,
        "startTime": start,
        "status": "SCHEDULED",
        "medicationAdministration": None,
        "notes": None,
        "patient": state["orders"][order]["patient"],
    }
    slot.update(kw)
    state["slots"].append(slot)
    return slot


def _admin(state: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    named = {"display": body["doseUnits"]} if body.get("doseUnits") else None
    return {
        "uuid": _u(),
        "status": body["status"],
        "notes": [] if state["adhoc_drops_notes"] else [{"text": n["text"]} for n in body["notes"]],
        # A java Date: epoch ms (the request carries seconds).
        "administeredDateTime": body["administeredDateTime"] * 1000,
        "doseUnits": None if state["null_dose_units"] else named,
        "route": {"display": body["route"]} if body.get("route") else None,
        "providers": [
            {"provider": {"uuid": p["providerUuid"]}, "function": {"display": p["function"]}}
            for p in body["providers"]
        ],
    }


def _wire_slot(state: dict[str, Any], slot: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in slot.items() if k != "patient"}
    if state["seconds_as_ms"]:
        out["startTime"] = slot["startTime"] * 1000
    return out


def _fake(state: dict[str, Any]) -> httpx.MockTransport:
    def schedule_get(params: httpx.QueryParams) -> httpx.Response:
        if state["schedule_403"]:
            return httpx.Response(403, json=PRIVILEGE_403)
        patient = params.get("patientUuid")
        if "orderUuids" in params:
            want = SERVICE_ANSWER.get(params.get("serviceType") or "MEDICATION_REQUEST")
            rows = [
                _wire_slot(state, s)
                for s in state["slots"]
                if s["order"]["uuid"] == params["orderUuids"] and s["serviceType"] == want
            ]
            return httpx.Response(200, json=rows)
        start, end = int(params["startTime"]), int(params["endTime"])
        rows = [
            _wire_slot(state, s)
            for s in state["slots"]
            if s["patient"] == patient and start <= s["startTime"] < end
        ]
        return httpx.Response(200, json=[{"slots": rows}] if rows else [])

    def schedule_post(body: dict[str, Any]) -> httpx.Response:
        state["schedule_posts"] += 1
        state.setdefault("schedule_bodies", []).append(body)
        order = state["orders"][body["orderUuid"]]
        if body["serviceType"] == "AS_NEEDED_PLACEHOLDER":
            _slot(state, order["uuid"], "AsNeededPlaceholder", order["activated_ms"] // 1000)
            return httpx.Response(200, json={"id": 1})
        times = (
            body["firstDaySlotsStartTime"]
            + body["remainingDaySlotsStartTime"]
            + body["dayWiseSlotsStartTime"]
        )
        if state["cap_slots"]:
            times = times[: math.ceil(order["quantity"] / order["dose"])]
        for t in times:
            _slot(state, order["uuid"], "MedicationRequest", t)
        return httpx.Response(200, json={"id": 1})

    def scheduled_admin(body: list[dict[str, Any]]) -> httpx.Response:
        out = []
        for req in body:
            slot = next(s for s in state["slots"] if s["uuid"] == req["slotUuid"])
            if slot["medicationAdministration"] is None:
                slot["medicationAdministration"] = _admin(state, req)
                slot["status"] = ADMIN_SLOT_STATUS[req["status"]]
            out.append(slot["medicationAdministration"])
        return httpx.Response(200, json=out)

    def adhoc_admin(body: dict[str, Any]) -> httpx.Response:
        state["adhoc_posts"] += 1
        admin = _admin(state, body)
        _slot(
            state,
            body["orderUuid"],
            "AsNeededMedicationRequest",
            body["administeredDateTime"],
            status="COMPLETED",
            medicationAdministration=admin,
        )
        return httpx.Response(200, json=admin)

    def encounter_post(body: dict[str, Any]) -> httpx.Response:
        if state["order_push_403"] and body.get("drugOrders"):
            return httpx.Response(403, json=PRIVILEGE_403)
        enc = {"uuid": body["encounterUuid"], "visit": body["visitUuid"], "obs": [], "orders": []}
        for d in body.get("drugOrders") or []:
            uuid = _u()
            dosing = d["dosingInstructions"]
            state["orders"][uuid] = {
                "uuid": uuid,
                "patient": body["patientUuid"],
                "as_needed": dosing["asNeeded"],
                "quantity": dosing["quantity"],
                "dose": dosing["dose"],
                "body": d,
                "activated_ms": 1793000000123,
            }
            enc["orders"].append(uuid)
        for o in body.get("observations") or []:
            members = o.get("groupMembers") or []
            if state["io_refuses_datetime"] and members:
                return httpx.Response(400, json={"error": {"message": "bad datetime"}})
            enc["obs"].append(
                {
                    "uuid": _u(),
                    "concept": o["concept"]["uuid"],
                    "value": o.get("value"),
                    "comment": o.get("comment"),
                    # Datetime obs come back as the record's date string.
                    "groupMembers": [
                        {
                            "value": (
                                datetime.fromtimestamp(m["value"] / 1000, UTC).strftime(
                                    "%Y-%m-%dT%H:%M:%S.000+0000"
                                )
                                if isinstance(m["value"], int) and m["value"] > 10**12
                                else m["value"]
                            ),
                            "concept": {"uuid": m["concept"]["uuid"]},
                        }
                        for m in members
                    ],
                }
            )
        state["encounters"][enc["uuid"]] = enc
        return httpx.Response(
            200,
            json={
                "encounterUuid": enc["uuid"],
                "observations": [{"uuid": o["uuid"]} for o in enc["obs"]],
            },
        )

    def encounter_get(uuid: str) -> httpx.Response:
        enc = state["encounters"].get(uuid)
        if enc is None:
            return httpx.Response(state["unknown_encounter_status"], json={"error": {}})
        return httpx.Response(
            200,
            json={
                "uuid": uuid,
                "visit": {"uuid": enc["visit"]},
                "orders": [{"uuid": o} for o in enc["orders"]],
                "obs": [
                    {
                        "uuid": o["uuid"],
                        "comment": o["comment"],
                        "voided": False,
                        "groupMembers": o["groupMembers"],
                    }
                    for o in enc["obs"]
                ],
            },
        )

    def obs_get(params: httpx.QueryParams) -> httpx.Response:
        rows = [
            {
                "uuid": o["uuid"],
                "value": float(o["value"]),
                "voided": False,
                "concept": {"uuid": o["concept"], "display": "LT-P4 Pulse"},
                "encounter": {"uuid": e["uuid"]},
            }
            for e in state["encounters"].values()
            for o in e["obs"]
            if o["concept"] == params["concept"]
        ]
        return httpx.Response(200, json={"results": rows})

    async def handle(req: httpx.Request) -> httpx.Response:
        path, method, params = req.url.path, req.method, req.url.params
        state["calls"].append((method, path))
        if path.endswith("/session"):
            return httpx.Response(
                200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=x"}
            )
        body = json.loads(req.content) if req.content else None
        auth = req.headers.get("authorization", "")
        user = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":")[0] if auth else ""
        if method == "GET" and path == f"{REST}/module":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"uuid": "ipd", "version": "1.1.1"},
                        {"uuid": "bedmanagement", "version": "6.0.0"},
                        {"uuid": "medicationadministration", "version": "1.0.0"},
                        {"uuid": "fhir2Extension", "version": "1.3.1"},
                    ]
                },
            )
        if method == "GET" and path == f"{REST}/bedtype":
            return httpx.Response(
                200, json={"results": [{"uuid": BED_TYPE, "name": n} for n in state["bed_types"]]}
            )
        if method == "DELETE" and path == f"{REST}/bedtype/{BED_TYPE}":
            state["bed_type_deleted"] = True
            return httpx.Response(204)
        if method == "GET" and path == f"{REST}/admissionLocation/{WARD}":
            return httpx.Response(200, json=_layout(state))
        if method == "DELETE" and path.startswith(f"{REST}/bed/"):
            uuid = path.rsplit("/", 1)[1]
            bed = next((b for b in state["beds"].values() if b["uuid"] == uuid), None)
            if bed is None:
                return httpx.Response(404, json={"error": {}})
            bed["deleted"] = True
            state["bed_deletes"].append(uuid)
            return httpx.Response(204)
        if method == "DELETE" and path == f"{REST}/location/{WARD}":
            state["ward_deleted"] = True
            return httpx.Response(204)
        if method == "GET" and path == f"{REST}/ipd/wards/{WARD}/patients":
            if user.startswith("lt-p4-"):
                state["probe_user"] = user
                return httpx.Response(403, json=PRIVILEGE_403)
            rows = [
                {
                    "patientDetails": {"uuid": p, "person": {"display": state["patients"][p]}},
                    "visitDetails": {"uuid": state["visits"][p]},
                    "bedDetails": {"bedId": b["id"], "bedNumber": b["number"]},
                }
                for b in state["beds"].values()
                for p in b["patients"]
            ]
            return httpx.Response(200, json={"totalPatients": len(rows), "activePatients": rows})
        if method == "POST" and path == f"{REST}/bahmnicore/bahmniencounter":
            return encounter_post(body)
        if method == "GET" and path.startswith(f"{REST}/encounter/"):
            return encounter_get(path.rsplit("/", 1)[1])
        if method == "GET" and path == f"{REST}/obs":
            return obs_get(params)
        if method == "GET" and path in (
            f"{REST}/concept/{INTAKE_SET}",
            f"{REST}/concept/{OUTPUT_SET}",
        ):
            return httpx.Response(200, json={"setMembers": state["io_members"]})
        if method == "GET" and path.startswith(f"{REST}/order/"):
            order = state["orders"].get(path.rsplit("/", 1)[1])
            if order is None:
                return httpx.Response(404, json={"error": {}})
            return httpx.Response(
                200,
                json={
                    "uuid": order["uuid"],
                    "dateActivated": datetime.fromtimestamp(
                        order["activated_ms"] / 1000, UTC
                    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    + "+0000",
                    "scheduledDate": None,
                },
            )
        if path == f"{REST}/ipd/schedule/type/medication":
            return schedule_get(params) if method == "GET" else schedule_post(body)
        if method == "POST" and path == f"{REST}/ipd/scheduledMedicationAdministrations":
            return scheduled_admin(body)
        if method == "POST" and path == f"{REST}/ipd/adhocMedicationAdministrations":
            return adhoc_admin(body)
        if method == "POST" and path == f"{REST}/person":
            state["person_body"] = body
            return httpx.Response(201, json={"uuid": "person-1"})
        if method == "POST" and path == f"{REST}/user":
            state["users"].append(body)
            return httpx.Response(201, json={"uuid": "user-1"})
        if method == "DELETE" and path == f"{REST}/user/user-1":
            state["user_deleted"] = True
            return httpx.Response(204)
        if method == "DELETE" and path == f"{REST}/person/person-1":
            state["person_deleted"] = True
            return httpx.Response(204)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


class _Adt:
    """The Phase 2 ADT calls, applied to the fake's beds."""

    def __init__(self, state: dict[str, Any]):
        self.state = state

    def _assignment(self, bed_id: int) -> RecordBedAssignment:
        bed = self.state["beds"][bed_id]
        return RecordBedAssignment(
            bed_id=bed_id,
            bed_uuid=bed["uuid"],
            bed_number=bed["number"],
            bed_type=bed["type"] or "",
            ward_uuid=WARD,
            ward_name="LT-P4 Ward",
        )

    async def admit(self, a: Any) -> RecordAdmitResult:
        visit = self.state["visits"].setdefault(a.patient_uuid, _u())
        self.state["beds"][a.bed_id]["patients"].append(a.patient_uuid)
        return RecordAdmitResult(
            visit_uuid=visit,
            encounter_uuid=_u(),
            bed=self._assignment(a.bed_id),
            created_encounter=True,
            new_visit=True,
            visit_stopped=False,
        )

    async def transfer(self, a: Any) -> RecordAdmitResult:
        _free(self.state, a.patient_uuid)
        self.state["beds"][a.bed_id]["patients"].append(a.patient_uuid)
        return RecordAdmitResult(
            visit_uuid=a.visit_uuid,
            encounter_uuid=_u(),
            bed=self._assignment(a.bed_id),
            created_encounter=True,
            new_visit=False,
            visit_stopped=False,
        )

    async def discharge(self, a: Any, *, end_visit: bool = True) -> RecordEncounterResult:
        _free(self.state, a.patient_uuid)
        return RecordEncounterResult(encounter_uuid=_u(), created=True)


def _ctx(state: dict[str, Any], tmp_path, monkeypatch, *, admin: bool = True) -> Any:
    monkeypatch.setenv("HARNESS_SAMPLES_DIR", str(tmp_path))
    http = BahmniHttp("https://b.test", "u", "p", transport=_fake(state))
    admin_http = BahmniHttp("https://b.test", "a", "p", transport=_fake(state)) if admin else None
    config = BahmniConfig(
        location_uuid=LOCATION,
        identifier_type_uuid="t",
        identifier_source_uuid="s",
        consultation_encounter_type_uuid=CONSULT_TYPE,
        visit_location_uuid=VISIT_LOCATION,
        drug_other_concept_uuid=DRUG_OTHER,
        drug_quantity_units="Tablet(s)",
    )
    x = adt_checks.AdtCtx(
        {"harness": {"provider_uuid": PROVIDER, "nursing": NURSING}}, http, config, admin_http
    )
    adt = _Adt(state)

    async def create_patient(p: Any) -> tuple[RecordPatient, bool]:
        uuid = _u()
        state["patients"][uuid] = f"{p.given} {p.family}"
        state.setdefault("patient_inputs", []).append(p)
        return (
            RecordPatient(
                uuid=uuid,
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

    async def create_ward(**kw: Any) -> RecordWard:
        state["ward_kw"] = kw
        return RecordWard(uuid=WARD, name=kw["name"], total_beds=0, occupied_beds=0, beds=())

    async def ensure_bed_type(*, name: str, display_name: str) -> str:
        state["bed_types"].append(name)
        return name

    async def add_bed(**kw: Any) -> RecordBed:
        bed_id = state["next_bed_id"]
        state["next_bed_id"] += 1
        bed = {
            "id": bed_id,
            "uuid": _u(),
            "number": kw["bed_number"],
            "type": kw["bed_type"],
            "column": kw["column"],
            "patients": [],
            "deleted": False,
        }
        state["beds"][bed_id] = bed
        state.setdefault("created_bed_uuids", {})[bed_id] = bed["uuid"]
        return RecordBed(
            bed_id=bed_id,
            bed_uuid=bed["uuid"],
            bed_number=bed["number"],
            bed_type=bed["type"],
            status="AVAILABLE",
            row=kw["row"],
            column=kw["column"],
            patient_uuids=(),
        )

    async def drop_patient(uuid: str) -> str | None:
        _free(state, uuid)
        state["dropped"].append(uuid)
        return None

    monkeypatch.setattr(x.record, "create_patient", create_patient)
    monkeypatch.setattr(x.record, "create_ward", create_ward)
    monkeypatch.setattr(x.record, "ensure_bed_type", ensure_bed_type)
    monkeypatch.setattr(x.record, "add_bed", add_bed)
    monkeypatch.setattr(x, "client", lambda **over: adt)
    monkeypatch.setattr(x, "_drop_patient", drop_patient)
    return x


async def _rows(state: dict[str, Any], tmp_path, monkeypatch, **kw: Any) -> dict[str, Any]:
    x = _ctx(state, tmp_path, monkeypatch, **kw)
    return {n: r for n, _t, r in await phase4_ipd.run_checks(x)}


# ─── the four named tests ────────────────────────────────────────────────────


async def test_run_checks_returns_one_row_per_check(tmp_path, monkeypatch):
    state = _state()
    x = _ctx(state, tmp_path, monkeypatch)
    rows = await phase4_ipd.run_checks(x)
    names = [n for n, _topic, _r in rows]
    assert names == [n for n, _topic, _fn in phase4_ipd.CHECKS]
    assert len(set(names)) == len(names) == 10
    statuses = {n: r[0] for n, _topic, r in rows}
    assert set(statuses.values()) == {"PASS"}, rows
    assert all(p.startswith("/openmrs/") for _m, p in state["calls"])
    assert x.cleanups == [], "run_checks leaves no cleanup for a later runner to repeat"


async def test_cleanup_deletes_beds_by_id(tmp_path, monkeypatch, capsys):
    state = _state()
    x = _ctx(state, tmp_path, monkeypatch)
    await phase4_ipd.run_checks(x)
    created = state["created_bed_uuids"]
    assert len(created) == 3 and all(b["deleted"] for b in state["beds"].values())
    # Every bed held a patient, so every create-time uuid is gone: the delete went to
    # the uuid read by bed id at cleanup time, never the one from create.
    assert len(state["bed_deletes"]) == 3
    assert not set(state["bed_deletes"]) & set(created.values())
    assert state["ward_deleted"] and state["bed_type_deleted"]
    assert len(state["dropped"]) == 2, "both synthetic patients voided"
    out = capsys.readouterr().out
    assert "CLEANUP INCOMPLETE" not in out
    assert len(re.findall(r"^CLEANUP bed (?!type)\S+ OK$", out, re.M)) == 3, out
    assert "FAILED" not in out, out


async def test_privilege_403_is_inconclusive_with_name(tmp_path, monkeypatch):
    rows = await _rows(_state(schedule_403=True), tmp_path, monkeypatch)
    for name in ("mar_schedule_create", "mar_privileges"):
        status, detail = rows[name]
        assert status == "INCONCLUSIVE", (name, rows[name])
        assert "Get Medication Tasks" in detail, (name, detail)
    # Checks that never reach the medication chart are not affected.
    assert rows["vitals_readback"][0] == "PASS"
    assert rows["ward_layout_occupants"][0] == "PASS"


def test_no_host_or_env_literals():
    src = (HARNESS / "phase4_ipd.py").read_text()
    for bad in ("os.environ", "getenv", "import os", "http://", "https://", "localhost"):
        assert bad not in src, f"phase4_ipd.py contains {bad!r}"
    assert not re.search(r"\bLT-P2-", src), "inpatient checks use LT-P4- names only"
    assert not re.search(r"\bP\d-L[V]-|\bL[V]-\d", src), "no internal check labels"
    hits = adt_checks.UUID_TOKEN.findall(src)
    assert not hits, f"uuid literals: {hits[:3]}"


# ─── hygiene ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [n for n, _t, _f in phase4_ipd.CHECKS])
def test_check_topics_are_slugs(name):
    topic = {n: t for n, t, _f in phase4_ipd.CHECKS}[name]
    assert re.fullmatch(r"[a-z0-9_]+", topic)


async def test_patients_wards_and_beds_are_lt_p4_named(tmp_path, monkeypatch):
    state = _state()
    x = _ctx(state, tmp_path, monkeypatch)
    await phase4_ipd.run_checks(x)
    assert len(state["patient_inputs"]) == 2
    for p in state["patient_inputs"]:
        assert p.given.startswith("LT-P4-") and p.family.startswith("LT-P4-")
    assert state["ward_kw"]["name"].startswith("LT-P4-")
    assert state["ward_kw"]["parent_location_uuid"] == VISIT_LOCATION
    assert all(n.startswith("LT-P4-") for n in state["bed_types"])
    # Bahmni's bed number column is 10 characters.
    assert all(len(b["number"]) <= 10 for b in state["beds"].values())
    assert state["users"][0]["username"].startswith("lt-p4-")
    assert state["user_deleted"] and state["person_deleted"]


# ─── each PASS condition also FAILs: one fake variant per guard ─────────────


async def test_bed_uuid_kept_is_recorded_not_failed(tmp_path, monkeypatch):
    rows = await _rows(_state(uuid_churn=False), tmp_path, monkeypatch)
    status, detail = rows["bed_identity_across_stay"]
    assert status == "PASS"
    assert "uuid_changed_after_transfer=False" in detail and "type_cleared=False" in detail


async def test_bed_uuid_churn_is_recorded(tmp_path, monkeypatch):
    rows = await _rows(_state(), tmp_path, monkeypatch)
    status, detail = rows["bed_identity_across_stay"]
    assert status == "PASS"
    assert "uuid_changed_after_transfer=True" in detail and "type_cleared=True" in detail


async def test_slot_count_not_capped_fails_schedule(tmp_path, monkeypatch):
    rows = await _rows(_state(cap_slots=False), tmp_path, monkeypatch)
    status, detail = rows["mar_schedule_create"]
    assert status == "FAIL" and "charted=4" in detail, detail


async def test_slot_time_in_ms_fails_schedule(tmp_path, monkeypatch):
    rows = await _rows(_state(seconds_as_ms=True), tmp_path, monkeypatch)
    assert rows["mar_schedule_create"][0] == "FAIL", rows["mar_schedule_create"]


async def test_schedule_create_records_raw_start_next_to_instant_sent(tmp_path, monkeypatch):
    state = _state()
    rows = await _rows(state, tmp_path, monkeypatch)
    status, detail = rows["mar_schedule_create"]
    sent = state["schedule_bodies"][0]
    first = min(sent["firstDaySlotsStartTime"])
    assert status == "PASS"
    assert f"sent_seconds={first} raw_startTime={first}" in detail, detail
    assert sent["medicationFrequency"] == "FIXED_SCHEDULE_FREQUENCY"
    assert sent["serviceType"] == "MEDICATION_REQUEST"
    assert state["schedule_bodies"][1]["serviceType"] == "AS_NEEDED_PLACEHOLDER"
    # Two creates (scheduled + as-needed); the repeated as-needed create sent nothing.
    assert state["schedule_posts"] == 2


async def test_null_dose_units_fails_naming_the_concept(tmp_path, monkeypatch):
    rows = await _rows(_state(null_dose_units=True), tmp_path, monkeypatch)
    status, detail = rows["mar_administration_idempotent"]
    assert status == "FAIL"
    assert "doseUnits=None" in detail and "Tablet(s)" in detail, detail


async def test_adhoc_marker_missing_fails(tmp_path, monkeypatch):
    state = _state(adhoc_drops_notes=True)
    rows = await _rows(state, tmp_path, monkeypatch)
    status, detail = rows["mar_adhoc_marker"]
    assert status == "FAIL" and "marker_returned=False" in detail, detail


async def test_adhoc_retry_posts_once(tmp_path, monkeypatch):
    state = _state()
    rows = await _rows(state, tmp_path, monkeypatch)
    assert rows["mar_adhoc_marker"][0] == "PASS"
    assert state["adhoc_posts"] == 1


async def test_unknown_encounter_500_fails_vitals(tmp_path, monkeypatch):
    rows = await _rows(_state(unknown_encounter_status=500), tmp_path, monkeypatch)
    status, detail = rows["vitals_readback"]
    assert status == "FAIL" and "unknown_encounter_status=500" in detail, detail


async def test_io_set_without_datetime_member_fails_naming_it(tmp_path, monkeypatch):
    state = _state()
    state["io_members"] = [_member("Text"), _member("Numeric")]
    rows = await _rows(state, tmp_path, monkeypatch)
    status, detail = rows["io_concepts_present"]
    assert status == "FAIL" and "Datetime" in detail, detail


async def test_io_datetime_refused_fails_with_the_switch(tmp_path, monkeypatch):
    rows = await _rows(_state(io_refuses_datetime=True), tmp_path, monkeypatch)
    status, detail = rows["io_concepts_present"]
    assert status == "FAIL" and "date string" in detail, detail


async def test_ward_auth_probe_skips_without_admin(tmp_path, monkeypatch):
    state = _state()
    rows = await _rows(state, tmp_path, monkeypatch, admin=False)
    assert rows["ward_endpoints_auth"][0] == "SKIP"
    assert state["users"] == []


async def test_ward_auth_probe_records_status_and_privilege(tmp_path, monkeypatch):
    state = _state()
    rows = await _rows(state, tmp_path, monkeypatch)
    status, detail = rows["ward_endpoints_auth"]
    assert status == "PASS" and "status=403" in detail and "Get Medication Tasks" in detail
    assert state["probe_user"].startswith("lt-p4-")
    # The probe's 403 is the point of that check, not a privilege finding for the login.
    assert rows["mar_privileges"][0] == "PASS"


async def test_raw_call_403_is_inconclusive_with_name(tmp_path, monkeypatch):
    # The harness's own raw calls (here the drug order push) report a 403 as a
    # privilege finding too, never as a module FAIL.
    rows = await _rows(_state(order_push_403=True), tmp_path, monkeypatch)
    status, detail = rows["mar_schedule_create"]
    assert status == "INCONCLUSIVE" and "Get Medication Tasks" in detail, detail
