"""The ``--adt`` check group of ``verify_adapter_live.py`` (the sm_common ADT surface).

Drives the real ``BahmniRecordClient`` ADT surface (wards, beds, worklists, dispositions,
admit, transfer, discharge, end visit) against a live Bahmni, plus raw probes for what
only the server can answer. One line per check:

    CHECK <name> PASS|FAIL|INCONCLUSIVE|SKIP <topic> <detail>

- ``PASS``: the expected behaviour holds. A record-only check passes once it has its value, which
  it prints after ``record``.
- ``FAIL``: the expected behaviour does not hold; the client needs its fallback for that topic.
- ``INCONCLUSIVE``: the probe could not decide: an HTTP 403 (a real privilege finding for
  the ADT login; the privilege Bahmni names is printed), or a check blocked because an
  earlier one did not complete.
- ``SKIP``: a box-wide probe that was not enabled.
Exit: 0 all PASS/SKIP; 1 a FAIL/INCONCLUSIVE or an early stop; 3 a box-wide change could
not be restored (``RESTORE FAILED`` lines carry the manual steps); 4 cleanup incomplete
(``LEFT`` lines); 130 interrupted.

Logins: the checks run as ``BAHMNI_USER`` (an API login with the ADT and bed privileges).
Cleanup and the box-wide probes run as ``BAHMNI_ADMIN_USER``/``BAHMNI_ADMIN_PASS`` (read once
by the caller).

Shared-box safety:
- Every record it creates is synthetic and named ``LT-P2-<RUN>`` (RUN: six random
  letters, printed first). It never touches the seed patient or any other record;
  a bed type it did not create is never deleted.
- Run order: read-only checks, then this run's own master data, then this run's own
  patients, and last the three checks that change box-wide state (``HARNESS_ADT_GLOBAL_STATE=1``
  plus the admin login; the stale-visit close also needs ``HARNESS_ADT_STALE_CLOSE=1``).
  Each writes the original to ``$HARNESS_SAMPLES_DIR/RESTORE-<RUN>.json`` (0600) before it
  changes anything, restores it in its own ``finally`` (verified by a read-back, two tries)
  and prints how long the box ran changed.
- SIGINT/SIGHUP/SIGTERM cancel the run like Ctrl-C, so restores and cleanup still run;
  run it inside tmux. ``HARNESS_DEADLINE_S`` (default 1200) bounds the checks, and three
  checks in a row with transport errors stop them; cleanup runs either way.
- Everything created is undone in a ``finally`` at the end, one ``CLEANUP`` line each:
  beds unassigned, visits ended, patients voided, beds deleted, locations retired, this
  run's bed type deleted. OpenMRS voids/retires rather than deletes, so per-run names keep
  reruns apart.

Detail text is meant to be pasted into a public PR: counts, booleans, stock names, status
codes and exception class names, never uuids (scrubbed), server messages or response
bodies; those go to ``HARNESS_SAMPLES_DIR`` only. ``RESTORE FAILED`` lines are for the
operator (they name uuids): do not paste them. No check retries automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import re
import signal
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import (
    AuthError,
    ConflictError,
    HmsAdapterError,
    TransientError,
)
from sm_common.integrations.record_types import (
    RecordAddress,
    RecordAdtInput,
    RecordBed,
    RecordEncounterInput,
    RecordObs,
    RecordPatientInput,
)

REST = "/openmrs/ws/rest/v1"
ENCOUNTER_FEED = "/openmrs/ws/atomfeed/encounter/recent"
ATOM = "{http://www.w3.org/2005/Atom}"
TAG = "LT-P2-"
# Letters only: OpenMRS person names may refuse digits, and no uuid-like run appears.
RUN = "".join(chr(ord("A") + int(ch, 16)) for ch in uuid4().hex[:6])
NAME = f"{TAG}{RUN}"
# Bahmni's bed.bed_number column is 10 characters on the live install (bed_number_max_len).
BEDP = f"P{RUN[:4]}"
GLOBAL_ENV = "HARNESS_ADT_GLOBAL_STATE"
STALE_ENV = "HARNESS_ADT_STALE_CLOSE"
# stack_module_versions: the stack the ADT client was verified against (module id -> version).
EXPECTED_MODULES = {
    "bedmanagement": "6.0.0",
    "bahmnicore": "1.2.0",
    "emrapi": "1.36.0",
    "ipd": "1.1.1",
}
WORKLIST_GPS = (
    "emrapi.sqlSearch.patientsToAdmit",
    "emrapi.sqlSearch.admittedPatients",
    "emrapi.sqlSearch.patientsToDischarge",
)
ADT_TYPE_NAMES = {"ADMISSION", "TRANSFER", "DISCHARGE"}
EMRAPI_SOURCE = "org.openmrs.module.emrapi"
DISPOSITION_NAMES = {"ADMIT": "Admit Patient", "DISCHARGE": "Discharge Patient"}
ADMISSION_TAG = "Admission Location"
UUID_TOKEN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\b(?=[A-Za-z]*\d)[0-9A-Za-z]{32,38}\b"
)
STALE_POLL_S = 30
# Running the stale-visit task is one synchronous call that returns only after every idle
# visit at a Visit Location is closed; on a box with many open visits that outlasts the
# client's 30 s default. HARNESS_ADT_STALE_TASK_TIMEOUT_S overrides it.
STALE_TASK_TIMEOUT_S = float(os.environ.get("HARNESS_ADT_STALE_TASK_TIMEOUT_S") or 600)

Result = tuple[str, str]
AdtCheck = Callable[["AdtCtx"], Awaitable[Result]]


def scrub(text: str) -> str:
    """Detail lines go to a public PR: no uuids."""
    return UUID_TOKEN.sub("<uuid>", text)


def passed(ok: bool, detail: str) -> Result:
    return ("PASS" if ok else "FAIL"), detail


def record(detail: str) -> Result:
    return "PASS", f"record {detail}"


def inconclusive(detail: str) -> Result:
    return "INCONCLUSIVE", detail


def iso(dt: datetime) -> str:
    """OpenMRS REST datetime, UTC."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def err(exc: BaseException) -> str:
    """Exception class and HTTP status only: messages can carry server text."""
    status = re.search(r"HTTP (\d{3})", str(exc))
    return type(exc).__name__ + (f"(HTTP {status.group(1)})" if status else "")


@dataclass
class Raw:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def results(self) -> list[dict[str, Any]]:
        rows = self.body.get("results") if isinstance(self.body, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    def shape(self) -> str:
        """Status plus what kind of body came back (never the body itself)."""
        if isinstance(self.body, dict):
            kind = "error" if "error" in self.body else f"keys={sorted(self.body)[:8]}"
        elif isinstance(self.body, list):
            kind = f"list[{len(self.body)}]"
        else:
            kind = "text" if self.body else "empty"
        return f"{self.status}:{kind}"


@dataclass
class Cleanup:
    priority: int  # 0 patients, 1 beds, 2 child locations, 3 locations, 4 bed types
    kind: str
    label: str
    fn: Callable[[], Awaitable[str | None]]  # None = done; "SKIPPED ..."; else why it failed


class AdtCtx:
    def __init__(
        self,
        cfg: dict[str, Any],
        http: BahmniHttp,
        config: BahmniConfig,
        admin_http: BahmniHttp | None = None,
    ):
        self.h: dict[str, Any] = cfg["harness"]
        self.http = http
        self.admin_http = admin_http
        self.config = config
        self.record = BahmniRecordClient(http, config)
        # Cleanup runs as the admin when one is given (void/retire privileges).
        self.admin_record = BahmniRecordClient(admin_http or http, config)
        self.provider: str = self.h["provider_uuid"]
        self.state: dict[str, Any] = {}
        self.samples = os.environ.get("HARNESS_SAMPLES_DIR")
        self.global_ok = os.environ.get(GLOBAL_ENV) == "1"
        self.cleanups: list[Cleanup] = []
        self.calls = 0
        self.writes = 0
        self.tap: list[tuple[str, str, int, Any]] = []
        # Encounters this run wrote into the main admission (ipd_side_effects tells them apart).
        self.ours: set[str] = set()
        self.restore_path = (
            pathlib.Path(self.samples) / f"RESTORE-{NAME}.json" if self.samples else None
        )
        if self.restore_path is not None:
            self.restore_path.parent.mkdir(parents=True, exist_ok=True)
        self.restore_entries: list[dict[str, Any]] = []
        self.restore_failed: list[dict[str, Any]] = []
        self.cancelled = False
        self.transport_error = False
        self._tap_send(http)
        if admin_http is not None:
            self._tap_send(admin_http)

    # ── plumbing ────────────────────────────────────────────────────────────

    def _tap_send(self, http: BahmniHttp) -> None:
        """Count every HTTP call and keep the recent responses for shape checks."""
        original = http._send

        async def tapped(method: str, path: str, **kw: Any) -> Any:
            self.calls += 1
            if method != "GET":
                self.writes += 1
            resp = await original(method, path, **kw)
            try:
                body = resp.json() if resp.content else None
            except ValueError:
                body = None
            self.tap.append((method, path, resp.status_code, body))
            del self.tap[:-80]
            return resp

        http._send = tapped  # type: ignore[method-assign]

    def last(self, method: str, path_suffix: str) -> tuple[int, Any] | None:
        for m, p, status, body in reversed(self.tap):
            if m == method and p.endswith(path_suffix):
                return status, body
        return None

    def sample(self, name: str, obj: Any) -> None:
        if not self.samples:
            return
        d = pathlib.Path(self.samples)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"adt_{name}.json").write_text(json.dumps(obj, indent=2, default=str))

    async def raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
        admin: bool = False,
        timeout_s: float | None = None,
    ) -> Raw:
        """One authenticated call that never raises on a status (5xx bodies kept).

        ``admin=True`` uses the admin login when one is configured. ``timeout_s``
        replaces the client's timeout for this call only.
        """
        extra: dict[str, Any] = {} if timeout_s is None else {"timeout": timeout_s}
        http = self.admin_http if admin and self.admin_http is not None else self.http
        cookie = http._cookie or await http._login(None)
        for attempt in (1, 2):
            resp = await http._send(
                method,
                path if path.startswith("/") else f"{REST}/{path}",
                params=params,
                json=body,
                auth=http._auth,
                headers={"Cookie": f"JSESSIONID={cookie}"},
                **extra,
            )
            if resp.status_code != 401 or attempt == 2:
                break
            cookie = await http._login(cookie)
        try:
            parsed: Any = resp.json() if resp.content else None
        except ValueError:
            parsed = resp.text
        return Raw(resp.status_code, parsed)

    def later(
        self, priority: int, kind: str, label: str, fn: Callable[[], Awaitable[str | None]]
    ) -> None:
        self.cleanups.append(Cleanup(priority, kind, label, fn))

    def client(self, **over: Any) -> BahmniRecordClient:
        return BahmniRecordClient(self.http, replace(self.config, **over))

    # ── synthetic data ──────────────────────────────────────────────────────

    async def new_patient(self, role: str) -> str:
        rec, _ = await self.record.create_patient(
            RecordPatientInput(
                marker=uuid4(),
                given=f"{NAME} {role}",
                family=NAME,
                gender="F",
                birth_date=date(1985, 1, 1),
                birth_date_estimated=False,
                phone=None,
                address=RecordAddress(),
            )
        )
        self.later(0, "patient", f"{NAME} {role}", lambda: self._drop_patient(rec.uuid))
        return rec.uuid

    async def _drop_patient(self, uuid: str) -> str | None:
        bed = await self.raw("GET", "beds", params={"patientUuid": uuid}, admin=True)
        for row in bed.results():
            got = await self.raw(
                "DELETE", f"beds/{row.get('bedId')}", params={"patientUuid": uuid}, admin=True
            )
            if not got.ok:
                return f"bed unassign {got.status}"
        visits = await self.raw(
            "GET",
            "visit",
            params={"patient": uuid, "includeInactive": "false", "v": "custom:(uuid)"},
            admin=True,
        )
        for v in visits.results():
            await self.admin_record.end_visit(str(v["uuid"]))
        got = await self.raw(
            "DELETE", f"patient/{uuid}", params={"reason": f"{TAG}harness cleanup"}, admin=True
        )
        return None if got.ok or got.status == 404 else f"void {got.status}"

    async def _delete(self, path: str, params: dict[str, str] | None = None) -> str | None:
        """Cleanup delete/retire, as the admin when one is configured."""
        got = await self.raw("DELETE", path, params=params or {"reason": "harness"}, admin=True)
        return None if got.ok or got.status == 404 else f"HTTP {got.status}"

    async def _bed_in_ward(self, bed_id: int, ward_uuid: str) -> RecordBed | None:
        ward = await self.record.get_ward(ward_uuid)
        if ward is None:
            raise RuntimeError(f"ward of bed {bed_id} unreadable")
        return next((b for b in ward.beds if b.bed_id == bed_id), None)

    async def _delete_bed(self, bed_id: int, ward_uuid: str | None = None) -> str | None:
        """Delete one of this run's mapped beds by its CURRENT uuid, read by id.

        Bahmni rewrites a bed's uuid each time a patient leaves it, so the uuid from create
        can be gone, and a 404 there must not read as "deleted". Done means confirmed by
        id: the bed is no longer in its ward layout (Bahmni's delete removes the bed's
        cell mapping before voiding it)."""
        where = ward_uuid or self.state["ward"]
        now = await self._bed_in_ward(bed_id, where)
        if now is None:
            return None
        got = await self.raw(
            "DELETE", f"bed/{now.bed_uuid}", params={"reason": "harness"}, admin=True
        )
        if await self._bed_in_ward(bed_id, where) is None:
            return None
        return f"HTTP {got.status}; bed still in its ward layout"

    def adt(
        self, patient: str, bed: RecordBed | None, visit: str | None, **kw: Any
    ) -> RecordAdtInput:
        return RecordAdtInput(
            event_id=uuid4(),
            patient_uuid=patient,
            provider_uuid=self.provider,
            location_uuid=self.config.location_uuid,
            bed_id=bed.bed_id if bed else 0,
            bed_uuid=bed.bed_uuid if bed else "",
            at=datetime.now(UTC),
            visit_uuid=visit,
            **kw,
        )

    async def gp(self, prop: str, admin: bool = False) -> tuple[str | None, str | None]:
        """(setting uuid, value) of a global property; (None, None) when absent."""
        got = await self.raw(
            "GET",
            "systemsetting",
            params={"q": prop, "v": "custom:(uuid,property,value)"},
            admin=admin,
        )
        for r in got.results():
            if r.get("property") == prop:
                value = r.get("value")
                return str(r.get("uuid")), value if isinstance(value, str) else None
        return None, None

    async def visit_state(self, visit: str) -> dict[str, Any]:
        got = await self.raw(
            "GET",
            f"visit/{visit}",
            params={
                "v": "custom:(uuid,stopDatetime,visitType:(uuid),"
                "attributes:(value,attributeType:(display)))"
            },
        )
        body = got.body if isinstance(got.body, dict) else {}
        attrs = {
            str((a.get("attributeType") or {}).get("display")): a.get("value")
            for a in body.get("attributes") or []
            if isinstance(a, dict)
        }
        vt = body.get("visitType") if isinstance(body.get("visitType"), dict) else {}
        return {
            "open": body.get("stopDatetime") is None and got.ok,
            "stop": body.get("stopDatetime"),
            "type": vt.get("uuid"),
            "attrs": attrs,
        }

    async def open_visits(self, patient: str) -> list[str]:
        got = await self.raw(
            "GET",
            "visit",
            params={"patient": patient, "includeInactive": "false", "v": "custom:(uuid)"},
        )
        return [str(r["uuid"]) for r in got.results()]

    async def bed_status(self, bed: RecordBed) -> tuple[str, tuple[str, ...]]:
        ward = await self.record.get_ward(self.state["ward"])
        hit = next((b for b in (ward.beds if ward else ()) if b.bed_id == bed.bed_id), None)
        return (hit.status, hit.patient_uuids) if hit else ("MISSING", ())


# ─── read-only ───────────────────────────────────────────────────────────────


async def module_versions(x: AdtCtx) -> Result:
    got = await x.raw("GET", "module", params={"v": "custom:(uuid,version,started)"})
    found = {
        str(r.get("uuid")).lower(): (r.get("version"), r.get("started")) for r in got.results()
    }
    parts, bad = [], []
    for mod, want in EXPECTED_MODULES.items():
        near_ids = [m for m in found if mod in m]
        key = mod if mod in found else (near_ids[0] if len(near_ids) == 1 else mod)
        ver, started = found.get(key, (None, None))
        if key != mod:
            parts.append(f"(id {key})")
        parts.append(f"{mod}={ver}{'' if started in (True, None) else '(stopped)'}")
        if ver is None:
            near = sorted(m for m in found if mod in m)
            bad.append(f"{mod} absent (ids like it: {near})")
        elif ver != want or started is False:
            bad.append(f"{mod} want {want}")
    return passed(got.ok and not bad, " ".join(parts) + (f" MISMATCH {bad}" if bad else ""))


async def adt_config_resolved(x: AdtCtx) -> Result:
    c = x.config
    resources = {
        "visit_location_uuid": "location",
        "ipd_visit_type_uuid": "visittype",
        "admission_encounter_type_uuid": "encountertype",
        "transfer_encounter_type_uuid": "encountertype",
        "discharge_encounter_type_uuid": "encountertype",
        "discharge_summary_encounter_type_uuid": "encountertype",
        "consultation_encounter_type_uuid": "encountertype",
        "note_marker_concept_uuid": "concept",
    }
    unset, unresolved, optional = [], [], []
    for key, resource in resources.items():
        value = getattr(c, key)
        if not value:
            # The summary type gates only discharge_summary_type, which FAILs on its own.
            (optional if key == "discharge_summary_encounter_type_uuid" else unset).append(key)
        elif not (await x.raw("GET", f"{resource}/{value}", params={"v": "custom:(uuid)"})).ok:
            unresolved.append(key)
    for kind in ("admit", "discharge"):
        if not c.disposition_codes.get(kind):
            unset.append(f"disposition_codes[{kind}]")
    notes = {k: bool(c.concept_map.get(k)) for k in ("disposition_note", "adt_notes")}
    tagged = False
    if c.visit_location_uuid:
        loc = await x.raw(
            "GET", f"location/{c.visit_location_uuid}", params={"v": "custom:(uuid,tags:(display))"}
        )
        tags = (loc.body or {}).get("tags") if isinstance(loc.body, dict) else None
        tagged = any(
            isinstance(t, dict) and str(t.get("display", "")).casefold() == "visit location"
            for t in tags or []
        )
    gps = {}
    for prop in (*WORKLIST_GPS, "encounter.feed.publish.url"):
        gps[prop.rsplit(".", 1)[-1]] = "set" if (await x.gp(prop))[1] else "UNSET"
    for prop in ("bahmni.encountersession.duration", "emrapi.visitExpireHours"):
        value = (await x.gp(prop))[1]
        gps[prop.rsplit(".", 1)[-1]] = (
            value if value and value.isdigit() else ("set" if value else "UNSET")
        )
    x.state["gps"] = gps
    ok = not unset and not unresolved and tagged
    return passed(
        ok,
        f"adt_config_keys unset={unset} unresolved={unresolved} unset_optional={optional} "
        f"disposition_mappings notes_mapped={notes} "
        f"visit_location_tagged={tagged} worklist_queries gps={gps}",
    )


async def disposition_mappings(x: AdtCtx) -> Result:
    names: dict[str, str | None] = {}
    for code in DISPOSITION_NAMES:
        rows: list[dict[str, Any]] = []
        for params in (
            {"source": EMRAPI_SOURCE, "code": code},
            {"term": f"{EMRAPI_SOURCE}:{code}"},
        ):
            got = await x.raw("GET", "concept", params={**params, "v": "custom:(uuid,display)"})
            rows = got.results()
            if rows:
                break
        names[code] = str(rows[0].get("display")) if len(rows) == 1 else None
    disp = await x.raw(
        "GET",
        "concept",
        params={"q": "Disposition", "v": "custom:(uuid,display,answers:(display))"},
    )
    concept = next(
        (r for r in disp.results() if str(r.get("display", "")).casefold() == "disposition"), None
    )
    answers = {str(a.get("display", "")).casefold() for a in (concept or {}).get("answers") or []}
    if any(v is None for v in names.values()):
        return inconclusive(f"mapping search found no single concept: {names}")
    mapped = all(
        (names[code] or "").casefold() == want.casefold()
        for code, want in DISPOSITION_NAMES.items()
    )
    are_answers = all(want.casefold() in answers for want in DISPOSITION_NAMES.values())
    config_names = all(
        x.config.disposition_codes.get(k, "").casefold() == DISPOSITION_NAMES[code].casefold()
        for k, code in (("admit", "ADMIT"), ("discharge", "DISCHARGE"))
    )
    return passed(
        mapped and are_answers and config_names,
        f"mapped={names} answers_of_disposition={are_answers} config_names_match={config_names}",
    )


async def type_names_exact(x: AdtCtx) -> Result:
    c = x.config
    want = {
        "admission_encounter_type_uuid": ("encountertype", "ADMISSION"),
        "transfer_encounter_type_uuid": ("encountertype", "TRANSFER"),
        "discharge_encounter_type_uuid": ("encountertype", "DISCHARGE"),
        "ipd_visit_type_uuid": ("visittype", "IPD"),
    }
    seen, bad = {}, []
    for key, (resource, name) in want.items():
        got = await x.raw("GET", f"{resource}/{getattr(c, key)}", params={"v": "custom:(name)"})
        actual = (got.body or {}).get("name") if isinstance(got.body, dict) else None
        seen[name] = actual
        if actual != name:
            bad.append(key)
    return passed(not bad, f"names={seen} not_exact={bad}")


async def emr_gps_set(x: AdtCtx) -> Result:
    c = x.config
    adm = (await x.gp("emr.admissionEncounterType"))[1]
    exit_ = (await x.gp("emr.exitFromInpatientEncounterType"))[1]
    a_ok = bool(adm) and adm == c.admission_encounter_type_uuid
    e_ok = bool(exit_) and exit_ == c.discharge_encounter_type_uuid
    return passed(
        a_ok and e_ok,
        f"admissionEncounterType_is_admission={a_ok} exitFromInpatient_is_discharge={e_ok}",
    )


async def unknown_ward_uuid(x: AdtCtx) -> Result:
    unknown = str(uuid4())
    ward = await x.raw("GET", f"admissionLocation/{unknown}", params={"v": "full"})
    loc = await x.raw("GET", f"location/{unknown}")
    via_client = await x.record.get_ward(unknown)
    return passed(
        loc.status == 404 and via_client is None,
        f"admissionLocation={ward.status} location={loc.status} get_ward_none={via_client is None}",
    )


# ─── this run's master data ──────────────────────────────────────────────────


async def ward_create(x: AdtCtx) -> Result:
    parent = x.config.visit_location_uuid
    ward = await x.record.create_ward(
        name=f"{NAME} Ward", parent_location_uuid=parent, rows=2, columns=3
    )
    x.state["ward"] = ward.uuid
    x.state["ward_create_resp"] = x.last("POST", "/admissionLocation")
    x.state["ward_layout_resp"] = x.last("POST", f"/admissionLocation/{ward.uuid}")
    x.later(
        3, "ward", f"{NAME} Ward", lambda: x._delete(f"location/{ward.uuid}", {"reason": "harness"})
    )
    wanted = f"{NAME}-GEN"
    # Only a type this run creates is ever deleted: Bahmni's name lookup may hand back an
    # existing type (bedtype_name_case), and bed types have no retire.
    existing = (await x.raw("GET", "bedtype", params={"name": wanted})).results()
    bed_type = await x.record.ensure_bed_type(name=wanted, display_name=f"P2{RUN[:4]}")
    x.state["bed_type"] = bed_type
    made = x.last("POST", "/bedtype")
    made_uuid = made[1].get("uuid") if made and isinstance(made[1], dict) else None
    created = not existing and bed_type == wanted and isinstance(made_uuid, str)
    if created:
        x.later(4, "bed type", bed_type, lambda: x._delete(f"bedtype/{made_uuid}"))
    else:

        async def keep() -> str | None:
            return "SKIPPED reused or unidentified, not ours: left in place"

        x.later(4, "bed type", bed_type, keep)
    beds = []
    for i, (row, col) in enumerate(((1, 1), (1, 2), (1, 3), (2, 1)), start=1):
        bed = await x.record.add_bed(
            ward_uuid=ward.uuid, bed_number=f"{BEDP}B{i}", bed_type=bed_type, row=row, column=col
        )
        x.later(1, "bed", bed.bed_number, lambda b=bed: x._delete_bed(b.bed_id))
        beds.append(bed)
    x.state["beds"] = beds
    x.state["bed_create_resp"] = x.last("POST", "/bed")
    return passed(
        ward.total_beds == 0 and len(beds) == 4,
        f"ward created (2x3) bed_type={'created' if created else 'REUSED (not deleted)'} beds=4",
    )


async def ward_layout(x: AdtCtx) -> Result:
    found = await x.record._ward_layout(x.state["ward"])
    if found is None:
        return passed(False, "ward not found")
    ward, cells = found
    want = {(b.row, b.column) for b in x.state["beds"]}
    got = {(b.row, b.column) for b in ward.beds}
    statuses = Counter(b.status for b in ward.beds)
    types_ok = all(b.bed_type == x.state["bed_type"] for b in ward.beds)
    return passed(
        len(cells) == 6 and got == want and statuses == Counter({"AVAILABLE": 4}) and types_ok,
        f"cells={len(cells)} beds={len(ward.beds)} statuses={dict(statuses)} types_ok={types_ok} "
        f"total={ward.total_beds} occupied={ward.occupied_beds}",
    )


def _has(body: Any, *path: str) -> bool:
    for key in path:
        if not isinstance(body, dict) or key not in body:
            return False
        body = body[key]
    return body is not None


async def ward_create_response_shape(x: AdtCtx) -> Result:
    out, ok = [], True
    for label in ("ward_create_resp", "ward_layout_resp"):
        hit = x.state[label]
        body = hit[1] if hit else None
        fields = {
            "ward.uuid": _has(body, "ward", "uuid"),
            "ward.display": _has(body, "ward", "display"),
            "totalBeds": _has(body, "totalBeds"),
            "occupiedBeds": _has(body, "occupiedBeds"),
        }
        ok = ok and hit is not None and all(fields.values())
        out.append(f"{label.split('_')[1]}={fields if hit else 'not called'}")
    return passed(ok, " ".join(out))


async def bed_create_response_shape(x: AdtCtx) -> Result:
    hit = x.state["bed_create_resp"]
    body = hit[1] if hit else None
    fields = {k: _has(body, k) for k in ("id", "uuid", "row", "column")}
    fields["bedType.name"] = _has(body, "bedType", "name")
    return passed(hit is not None and all(fields.values()), f"fields={fields}")


async def ward_name_case(x: AdtCtx) -> Result:
    name = f"{NAME} Ward".swapcase()
    got = await x.raw(
        "POST",
        "admissionLocation",
        body={"name": name, "parentLocationUuid": x.config.visit_location_uuid},
    )
    if got.ok:
        uuid = (got.body.get("ward") or {}).get("uuid") if isinstance(got.body, dict) else None
        # Retire it now: a second same-name ward would confuse later lookups by name.
        why = await x._delete(f"location/{uuid}", {"reason": "harness"}) if uuid else "no uuid"
        if why is not None and uuid:
            x.later(3, "ward", name, lambda: x._delete(f"location/{uuid}", {"reason": "harness"}))
        return record(
            f"case-variant name ACCEPTED ({got.status}): duplicate check is case-sensitive; "
            f"retired_now={why is None}"
        )
    duplicate = "location.duplicate.name" in json.dumps(got.body)
    return record(
        f"case-variant name refused ({got.status}, duplicate_name={duplicate}): case-insensitive"
    )


async def ward_layout_shrink(x: AdtCtx) -> Result:
    parent, ward_uuid = x.config.visit_location_uuid, x.state["ward"]
    before = x.writes
    again = await x.record.create_ward(
        name=f"{NAME} Ward", parent_location_uuid=parent, rows=2, columns=3
    )
    same_noop = again.uuid == ward_uuid and x.writes == before
    before = x.writes
    try:
        await x.record.create_ward(
            name=f"{NAME} Ward", parent_location_uuid=parent, rows=1, columns=1
        )
        refused = "not refused"
    except ConflictError as exc:
        refused = str(exc)
    shrink_ok = refused == "ward_layout_shrink_blocked" and x.writes == before
    raw = await x.raw(
        "POST", f"admissionLocation/{ward_uuid}", body={"bedLayout": {"row": 1, "column": 1}}
    )
    found = await x.record._ward_layout(ward_uuid)
    intact = found is not None and len(found[1]) == 6 and len(found[0].beds) == 4
    return passed(
        same_noop and shrink_ok and raw.status >= 500 and intact,
        f"same_layout_noop={same_noop} client_shrink={refused} zero_writes={shrink_ok} "
        f"raw_shrink={raw.status} layout_unchanged={intact}",
    )


async def ward_layout_on_untagged_location(x: AdtCtx) -> Result:
    loc = await x.raw("POST", "location", body={"name": f"{NAME} Untagged"})
    uuid = loc.body.get("uuid") if isinstance(loc.body, dict) else None
    if not uuid:
        return inconclusive(f"could not create the untagged location ({loc.status})")
    x.state["untagged"] = uuid
    x.later(
        3,
        "location",
        f"{NAME} Untagged",
        lambda: x._delete(f"location/{uuid}", {"reason": "harness"}),
    )
    got = await x.raw(
        "POST", f"admissionLocation/{uuid}", body={"bedLayout": {"row": 1, "column": 1}}
    )
    x.sample("layout_untagged", got.body)
    return record(f"layout POST on an untagged location -> {got.shape()}")


async def bed_create_orphan_on_cell_conflict(x: AdtCtx) -> Result:
    number = f"{BEDP}OR"
    got = await x.raw(
        "POST",
        "bed",
        body={
            "bedNumber": number,
            "bedType": x.state["bed_type"],
            "row": 1,
            "column": 1,
            "locationUuid": x.state["ward"],
        },
    )
    made = got.body.get("uuid") if got.ok and isinstance(got.body, dict) else None
    if made:
        # No conflict after all (a bed now shares the cell): never leave it behind. It
        # sits in a cell of this run's ward, so delete it by id (its uuid may change).
        made_id = got.body.get("id")
        if isinstance(made_id, int):
            x.later(1, "bed", number, lambda: x._delete_bed(made_id))
        else:
            x.later(1, "bed", number, lambda: x._delete(f"bed/{made}"))
        return passed(False, f"record race POST -> {got.status}: accepted, no 400")
    orphan = None
    start = 0
    for _ in range(40):
        page = await x.raw(
            "GET", "bed", params={"v": "custom:(uuid,bedNumber)", "startIndex": str(start)}
        )
        rows = page.results()
        orphan = next((r for r in rows if r.get("bedNumber") == number), None)
        if orphan or not rows:
            break
        start += len(rows)
    if orphan is None:
        return passed(
            got.status == 400, f"record race POST -> {got.status}; no orphan bed row found"
        )
    # The delete a caller's cleanup would run: as the ADT login.
    by_api = await x.raw("DELETE", f"bed/{orphan['uuid']}")
    deleted = None if by_api.ok else f"HTTP {by_api.status}"
    gone = (await x.raw("GET", f"bed/{orphan['uuid']}")).status == 404
    if deleted is not None or not gone:
        x.later(1, "bed", number, lambda: x._delete(f"bed/{orphan['uuid']}"))
    return passed(
        got.status == 400 and gone,
        f"record race POST -> {got.status}; orphan_row_left=True "
        f"delete={'ok' if deleted is None else deleted} "
        f"gone_after_delete={gone}",
    )


async def bedtype_name_case(x: AdtCtx) -> Result:
    lower = x.state["bed_type"].lower()
    search = await x.raw("GET", "bedtype", params={"name": lower})
    bed = await x.raw(
        "POST",
        "bed",
        body={
            "bedNumber": f"{BEDP}CS",
            "bedType": lower,
            "row": 2,
            "column": 2,
            "locationUuid": x.state["ward"],
        },
    )
    uuid = bed.body.get("uuid") if isinstance(bed.body, dict) else None
    bed_id = bed.body.get("id") if isinstance(bed.body, dict) else None
    if isinstance(bed_id, int):
        # Mapped at (2,2) of this run's ward, so a later check may put a patient in it.
        x.later(1, "bed", f"{NAME}-CASE", lambda: x._delete_bed(bed_id))
    elif uuid:
        x.later(1, "bed", f"{NAME}-CASE", lambda: x._delete(f"bed/{uuid}"))
    return record(
        f"bedtype?name=<lowercase> rows={len(search.results())}; "
        f"bed create with lowercase type -> {bed.status}"
    )


async def location_duplicate_retired_first(x: AdtCtx) -> Result:
    name = f"{NAME} Dup"
    first = await x.raw("POST", "location", body={"name": name})
    u1 = first.body.get("uuid") if isinstance(first.body, dict) else None
    if not u1:
        return inconclusive(f"could not create the first location ({first.status})")
    x.later(3, "location", name, lambda: x._delete(f"location/{u1}", {"reason": "harness"}))
    retired = await x.raw("DELETE", f"location/{u1}", params={"reason": "harness"})
    second = await x.raw("POST", "location", body={"name": name})
    u2 = second.body.get("uuid") if isinstance(second.body, dict) else None
    if u2:
        x.later(3, "location", name, lambda: x._delete(f"location/{u2}", {"reason": "harness"}))
    x.state["dup"] = (name, u1, u2)
    return record(
        f"retire={retired.status}; same name after retire -> {second.status} "
        f"({'reusable' if u2 else 'still blocked'})"
    )


async def ward_location_search_rep(x: AdtCtx) -> Result:
    """The rep create_ward's duplicate-name fallback reads (ward_location_search_rep)."""
    name, retired_uuid, live_uuid = x.state["dup"]
    rep = "custom:(uuid,name,retired,parentLocation:(uuid),tags:(name))"

    async def search(q: str) -> list[dict[str, Any]]:
        got = await x.raw("GET", "location", params={"q": q, "v": rep})
        x.sample(f"location_search_{len(q)}", got.body)
        return got.results() if got.ok else []

    full = await search(name)
    ours = {retired_uuid, live_uuid} - {None}
    parses = bool(full) and all({"uuid", "name", "retired", "tags"} <= set(r) for r in full)
    prefix = any(r.get("uuid") in ours for r in await search(name[: len(name) - 2]))
    infix = any(r.get("uuid") in ours for r in await search(name[3:]))
    retired_listed = any(r.get("uuid") == retired_uuid for r in full)
    return passed(
        parses,
        f"rep_parses={parses} q_prefix_matches={prefix} q_infix_matches={infix} "
        f"retired_rows_listed={retired_listed}",
    )


async def ward_duplicate_name_400_shape(x: AdtCtx) -> Result:
    """A duplicate ward name is refused (400 location.duplicate.name, or a bare 500 on
    some installs), and the client handles both by location search
    (ward_duplicate_name_refusal): the same name under the same parent reuses this run's
    ward, under another parent it is ``ward_name_taken``; neither writes."""
    got = await x.raw(
        "POST",
        "admissionLocation",
        body={"name": f"{NAME} Ward", "parentLocationUuid": x.config.visit_location_uuid},
    )
    x.sample("duplicate_name_400", got.body)
    if got.ok:
        ward = (got.body.get("ward") or {}) if isinstance(got.body, dict) else {}
        uuid = ward.get("uuid")
        why = await x._delete(f"location/{uuid}") if uuid else "no uuid"
        if why is not None and uuid:
            x.later(3, "ward", f"{NAME} Ward (duplicate)", lambda: x._delete(f"location/{uuid}"))
        return passed(
            False, f"duplicate ward name ACCEPTED ({got.status}); retired_now={why is None}"
        )
    token = "location.duplicate.name" in json.dumps(got.body)
    refused = (got.status == 400 and token) or got.status >= 500
    before = x.writes
    reused = await x.record.create_ward(
        name=f"{NAME} Ward", parent_location_uuid=x.config.visit_location_uuid, rows=2, columns=3
    )
    reuse_ok = reused.uuid == x.state["ward"] and x.writes == before
    before = x.writes
    try:
        # Any other existing location as the parent: the ward itself.
        stray = await x.record.create_ward(
            name=f"{NAME} Ward", parent_location_uuid=x.state["ward"], rows=2, columns=3
        )
        other_parent = "accepted"
        if stray.uuid != x.state["ward"]:
            x.later(
                3,
                "ward",
                f"{NAME} Ward (other parent)",
                lambda: x._delete(f"location/{stray.uuid}", {"reason": "harness"}),
            )
    except ConflictError as exc:
        other_parent = str(exc)
    other_ok = other_parent == "ward_name_taken" and x.writes == before
    return passed(
        refused and reuse_ok and other_ok,
        f"raw_status={got.status} carries_token={token} client_same_parent_reused={reuse_ok} "
        f"client_other_parent={other_parent} zero_writes={x.writes == before}",
    )


async def find_by_marker_live(x: AdtCtx) -> Result:
    mine, near = uuid4(), uuid4()
    rec, _ = await x.record.create_patient(
        RecordPatientInput(
            marker=mine,
            given=f"{NAME} Marker",
            family=NAME,
            gender="M",
            birth_date=date(1990, 1, 1),
            birth_date_estimated=False,
            phone=None,
            address=RecordAddress(),
        )
    )
    x.later(0, "patient", f"{NAME} Marker", lambda: x._drop_patient(rec.uuid))
    other, _ = await x.record.create_patient(
        RecordPatientInput(
            marker=near,
            given=f"{NAME} Near",
            family=NAME,
            gender="M",
            birth_date=date(1990, 1, 2),
            birth_date_estimated=False,
            phone=None,
            address=RecordAddress(),
        )
    )
    x.later(0, "patient", f"{NAME} Near", lambda: x._drop_patient(other.uuid))
    # Turn the second marker into a LIKE near-match of the first.
    attrs = await x.raw(
        "GET", f"person/{other.uuid}/attribute", params={"v": "custom:(uuid,attributeType:(uuid))"}
    )
    attr = next(
        (
            a
            for a in attrs.results()
            if (a.get("attributeType") or {}).get("uuid") == x.config.marker_attribute_type_uuid
        ),
        None,
    )
    if attr is None:
        return inconclusive("near-match patient carries no marker attribute")
    near_value = f"sm:{mine}-near"
    upd = await x.raw(
        "POST", f"person/{other.uuid}/attribute/{attr['uuid']}", body={"value": near_value}
    )
    x.sample("near_match_update", upd.body)
    # Judge the write by reading it back: the update can land and still answer non-2xx.
    back = await x.raw(
        "GET", f"person/{other.uuid}/attribute/{attr['uuid']}", params={"v": "custom:(value)"}
    )
    written = isinstance(back.body, dict) and back.body.get("value") == near_value
    raw = await x.raw(
        "GET",
        "bahmni/search/patient",
        params={
            "q": "",
            "s": "byIdOrNameOrVillage",
            "customAttribute": f"sm:{mine}",
            "patientAttributes": x.config.marker_attribute_name,
            "patientSearchResultsConfig": x.config.marker_attribute_name,
        },
    )
    raw_hits = len((raw.body or {}).get("pageOfResults") or []) if isinstance(raw.body, dict) else 0
    hits = await x.record.find_by_marker(mine)
    ok = [h.uuid for h in hits] == [rec.uuid]
    # The exclusion is only shown when Bahmni's LIKE search really returned the near-match.
    return passed(
        ok and written and raw_hits >= 2 and other.uuid not in {h.uuid for h in hits},
        f"found_exactly_seeded={ok} near_match_written={written} update_status={upd.status} "
        f"raw_like_hits={raw_hits} client_hits={len(hits)}",
    )


# ─── this run's patients: the ADT flow ───────────────────────────────────────


async def disposition_admit_lists_to_admit(x: AdtCtx) -> Result:
    patient = await x.new_patient("Admit")
    visit, _ = await x.record.start_visit(patient)
    x.state.update(pA=patient, opdA=visit.uuid)
    note = "LT-P2 admit advice" if x.config.concept_map.get("disposition_note") else None
    r = await x.record.write_disposition(
        event_id=uuid4(),
        patient_uuid=patient,
        visit_uuid=visit.uuid,
        provider_uuid=x.provider,
        kind="admit",
        at=datetime.now(UTC),
        note=note,
    )
    rows = await x.record.adt_worklist("to_admit")
    listed = any(r_.patient_uuid == patient and r_.visit_uuid == visit.uuid for r_ in rows)
    # Bahmni's stock query lists a visit only when its own location IS the Visit Location.
    got = await x.raw("GET", f"visit/{visit.uuid}", params={"v": "custom:(location:(uuid))"})
    loc = (got.body.get("location") or {}) if isinstance(got.body, dict) else {}
    at_visit_location = loc.get("uuid") == x.config.visit_location_uuid
    return passed(
        r.created and listed,
        f"disposition_created={r.created} note_sent={note is not None} listed={listed} "
        f"visit_at_visit_location={at_visit_location}",
    )


async def admit_new_ipd_visit(x: AdtCtx) -> Result:
    patient, opd = x.state["pA"], x.state["opdA"]
    open_before = len(await x.open_visits(patient))
    bed1 = x.state["beds"][0]
    a = x.adt(patient, bed1, opd)
    client = x.client(admit_new_ipd_visit=True)
    r = await client.admit(a)
    x.state.update(admA=a, admA_enc=r.encounter_uuid, ipdA=r.visit_uuid)
    x.ours.add(r.encounter_uuid)
    conv = x.last("POST", "/endVisitAndCreateEncounter")
    carried = conv is not None and _has(conv[1], "visitUuid")
    old, new = await x.visit_state(opd), await x.visit_state(r.visit_uuid)
    attrs = new["attrs"]
    ok = (
        open_before == 1
        and r.new_visit
        and r.created_encounter
        and carried
        and not old["open"]
        and new["open"]
        and new["type"] == x.config.ipd_visit_type_uuid
        and attrs.get("Admission Status") == "Admitted"
        and attrs.get("Visit Status") == "IPD"
        and r.bed.bed_id == bed1.bed_id
    )
    return passed(
        ok,
        f"open_visits_before={open_before} new_visit={r.new_visit} response_visitUuid={carried} "
        f"opd_stopped={not old['open']} new_is_ipd={new['type'] == x.config.ipd_visit_type_uuid} "
        f"admission_status={attrs.get('Admission Status')} "
        f"visit_status={attrs.get('Visit Status')} "
        f"bed_assigned={r.bed.bed_id == bed1.bed_id}",
    )


async def admit_retry_same_encounter(x: AdtCtx) -> Result:
    before = x.writes
    r = await x.client(admit_new_ipd_visit=True).admit(x.state["admA"])
    ok = (
        not r.created_encounter
        and r.encounter_uuid == x.state["admA_enc"]
        and r.visit_uuid == x.state["ipdA"]
        and x.writes == before
    )
    return passed(
        ok,
        f"created={r.created_encounter} same_encounter={r.encounter_uuid == x.state['admA_enc']} "
        f"same_visit={r.visit_uuid == x.state['ipdA']} writes={x.writes - before}",
    )


async def bed_occupied_after_admit(x: AdtCtx) -> Result:
    bed1 = x.state["beds"][0]
    status, patients = await x.bed_status(bed1)
    current = await x.record.current_bed(x.state["pA"])
    admitted = await x.record.adt_worklist("admitted")
    listed = any(r.patient_uuid == x.state["pA"] for r in admitted)
    ok = status == "OCCUPIED" and x.state["pA"] in patients and current is not None
    ok = ok and current.bed_id == bed1.bed_id and listed
    return passed(
        ok,
        f"bed_status={status} patient_on_bed={x.state['pA'] in patients} "
        f"current_bed_matches={bool(current and current.bed_id == bed1.bed_id)} "
        f"admitted_listed={listed}",
    )


async def adt_visit_rep_parses(x: AdtCtx) -> Result:
    st = await x.record._adt_visit(x.state["ipdA"])
    x.sample("adt_visit_rep", st)
    encounters = [e.get("uuid") for e in st["encounters"] if isinstance(e, dict)]
    ok = (
        st.get("stopDatetime") is None
        and (st.get("patient") or {}).get("uuid") == x.state["pA"]
        and (st.get("visitType") or {}).get("uuid") == x.config.ipd_visit_type_uuid
        and x.state["admA_enc"] in encounters
    )
    return passed(
        ok,
        f"parses=True stop_null_when_open={st.get('stopDatetime') is None} "
        f"encounters={len(encounters)} admission_listed={x.state['admA_enc'] in encounters}",
    )


async def visit_uuid_kept(x: AdtCtx) -> Result:
    patient = await x.new_patient("Keep")
    visit, _ = await x.record.start_visit(patient)
    bed2 = x.state["beds"][1]
    a = x.adt(patient, bed2, visit.uuid)
    r = await x.client(admit_new_ipd_visit=False).admit(a)
    x.state.update(pB=patient, visitB=visit.uuid, admB_enc=r.encounter_uuid)
    st = await x.visit_state(visit.uuid)
    attrs = st["attrs"]
    # admit_encounter, the admit-into-open-visit path: Bahmni sets the same visit attributes.
    attrs_ok = attrs.get("Admission Status") == "Admitted" and attrs.get("Visit Status") == "IPD"
    ok = r.visit_uuid == visit.uuid and not r.new_visit and st["open"] and attrs_ok
    return passed(
        ok,
        f"admit_kept_visit={r.visit_uuid == visit.uuid} new_visit={r.new_visit} "
        f"still_open={st['open']} "
        f"type_unchanged={st['type'] != x.config.ipd_visit_type_uuid} "
        f"admit_encounter admission_status={attrs.get('Admission Status')} "
        f"visit_status={attrs.get('Visit Status')} "
        "(transfer/discharge: see transfer_moves_bed, discharge_frees_bed)",
    )


async def second_patient_same_bed_refused_by_client(x: AdtCtx) -> Result:
    bed1, bed2 = x.state["beds"][0], x.state["beds"][1]
    patient = await x.new_patient("Second")
    before = x.writes
    try:
        await x.record.admit(x.adt(patient, bed1, None))
        refused = "not refused"
    except ConflictError as exc:
        refused = str(exc)
    zero_writes = x.writes == before
    client_ok = refused == "bed_occupied" and zero_writes and not await x.open_visits(patient)
    # What the server itself does with a second patient, holding NO bed, on an occupied
    # bed (bed_single_occupancy): this patient gets a visit and a raw ADMISSION, then a raw assign.
    visit, _ = await x.record.start_visit(patient, x.config.ipd_visit_type_uuid)
    adm = await x.raw(
        "POST", "bahmnicore/bahmniencounter", body=_admission_body(x, patient, visit.uuid)
    )
    enc = adm.body.get("encounterUuid") if isinstance(adm.body, dict) else None
    if not enc:
        return inconclusive(
            f"client={refused} zero_writes={zero_writes}; raw ADMISSION {adm.status}"
        )
    second = await x.raw(
        "POST", f"beds/{bed1.bed_id}", body={"patientUuid": patient, "encounterUuid": enc}
    )
    if second.ok:
        x.state["assign_resp"] = second.body
    _, on_bed1 = await x.bed_status(bed1)
    off = await x.raw("DELETE", f"beds/{bed1.bed_id}", params={"patientUuid": patient})
    _, after = await x.bed_status(bed1)
    restored = after == (x.state["pA"],)
    _ = bed2
    return passed(
        client_ok and restored,
        f"client={refused} zero_writes={zero_writes} server_second_assign={second.status} "
        f"patients_on_bed_then={len(on_bed1)} unassign={off.status} first_patient_kept={restored}",
    )


async def bed_read_shape(x: AdtCtx) -> Result:
    """The client reads a bed by id (``beds/<id>`` + the ward layout), never by uuid
    (bed_read_by_id). Runs right after bed_single_occupancy freed bed 1 of a second
    patient: ``record`` whether Bahmni rewrote bed 1's uuid (and cleared its bed type) on
    that unassign."""
    bed1 = x.state["beds"][0]
    got = await x.raw("GET", f"beds/{bed1.bed_id}")
    x.sample("bed_details_by_id", got.body)
    body = got.body if isinstance(got.body, dict) else {}
    id_ok = isinstance(body.get("bedId"), int) and body.get("bedId") == bed1.bed_id
    ward_ok = isinstance((body.get("physicalLocation") or {}).get("uuid"), str)
    patients_list = isinstance(body.get("patients"), list)
    unknown = await x.raw("GET", "beds/999999999")
    churn = await _bed_uuid_churn(x, bed1)
    return passed(
        got.status == 200 and id_ok and ward_ok and patients_list and unknown.status == 404,
        f"by_id={got.status} bedId_int={id_ok} physicalLocation={ward_ok} "
        f"patients_list={patients_list} unknown_id={unknown.status} record {churn}",
    )


async def _bed_uuid_churn(x: AdtCtx, bed: RecordBed) -> str:
    """For a bed a patient has left: does ``bed/<uuid as created>`` still answer, did the
    layout's uuid change, and what bed type does the layout show now."""
    by_old = await x.raw("GET", f"bed/{bed.bed_uuid}")
    x.sample(f"bed_by_created_uuid_{bed.bed_number}", by_old.body)
    ward = await x.record.get_ward(x.state["ward"])
    now = next((b for b in (ward.beds if ward else ()) if b.bed_id == bed.bed_id), None)
    changed = now is not None and now.bed_uuid != bed.bed_uuid
    type_after = (now.bed_type or "null") if now else "missing"
    return (
        f"created_uuid_get={by_old.status} layout_uuid_changed={changed} "
        f"bed_type_after={'same' if type_after == bed.bed_type else type_after}"
    )


async def bed_assign_response_shape(x: AdtCtx) -> Result:
    body = x.state["assign_resp"]
    fields = {
        "bedId": _has(body, "bedId"),
        "bedNumber": _has(body, "bedNumber"),
        "bedType.name": _has(body, "bedType", "name"),
        "physicalLocation.uuid": _has(body, "physicalLocation", "uuid"),
        "physicalLocation.display": _has(body, "physicalLocation", "display"),
    }
    return passed(all(fields.values()), f"fields={fields}")


async def bed_assign_unknown_id_rollback(x: AdtCtx) -> Result:
    p_b, bed2 = x.state["pB"], x.state["beds"][1]
    unknown = -(1 + uuid4().int % 1000)  # bed ids are positive: never a real bed
    got = await x.raw(
        "POST", f"beds/{unknown}", body={"patientUuid": p_b, "encounterUuid": x.state["admB_enc"]}
    )
    current = await x.record.current_bed(p_b)
    intact = current is not None and current.bed_id == bed2.bed_id
    return passed(
        intact and not got.ok, f"unknown_bed_id_assign={got.status} previous_bed_intact={intact}"
    )


async def transfer_moves_bed(x: AdtCtx) -> Result:
    bed1, bed3 = x.state["beds"][0], x.state["beds"][2]
    r = await x.record.transfer(x.adt(x.state["pA"], bed3, x.state["ipdA"]))
    x.state["transfer1_enc"] = r.encounter_uuid
    x.ours.add(r.encounter_uuid)
    current = await x.record.current_bed(x.state["pA"])
    old_status, _ = await x.bed_status(bed1)
    st = await x.record._adt_visit(x.state["ipdA"])
    in_visit = any(
        e.get("uuid") == r.encounter_uuid for e in st["encounters"] if isinstance(e, dict)
    )
    ok = (
        r.created_encounter
        and current is not None
        and current.bed_id == bed3.bed_id
        and old_status == "AVAILABLE"
        and r.visit_uuid == x.state["ipdA"]
        and in_visit
    )
    return passed(
        ok,
        f"moved={bool(current and current.bed_id == bed3.bed_id)} old_bed={old_status} "
        f"admission_visit_reuse transfer_in_given_visit={in_visit}",
    )


async def transfer_merge_window(x: AdtCtx) -> Result:
    bed4 = x.state["beds"][3]
    r = await x.record.transfer(x.adt(x.state["pA"], bed4, x.state["ipdA"]))
    merged = r.encounter_uuid == x.state["transfer1_enc"]
    x.ours.add(r.encounter_uuid)
    current = await x.record.current_bed(x.state["pA"])
    moved = current is not None and current.bed_id == bed4.bed_id
    session = x.state.get("gps", {}).get("duration", "?")
    return (
        "PASS" if moved else "FAIL",
        f"record second_transfer_merged={merged} session_duration={session} moved_again={moved}",
    )


async def child_location_bed_current_bed(x: AdtCtx) -> Result:
    room = await x.record.create_ward(
        name=f"{NAME} Room", parent_location_uuid=x.state["ward"], rows=1, columns=1
    )
    x.later(
        2, "room", f"{NAME} Room", lambda: x._delete(f"location/{room.uuid}", {"reason": "harness"})
    )
    bed = await x.record.add_bed(
        ward_uuid=room.uuid, bed_number=f"{BEDP}R1", bed_type=x.state["bed_type"], row=1, column=1
    )
    x.later(1, "bed", bed.bed_number, lambda: x._delete_bed(bed.bed_id, room.uuid))
    try:
        r = await x.record.transfer(x.adt(x.state["pB"], bed, x.state["visitB"]))
        moved = r.bed.bed_id == bed.bed_id
    except HmsAdapterError as exc:
        return passed(False, f"transfer into a child-location bed -> {err(exc)}")
    current = await x.record.current_bed(x.state["pB"])
    resolved = (
        current is not None and current.bed_id == bed.bed_id and current.ward_uuid == room.uuid
    )
    parent = await x.record.get_ward(x.state["ward"])
    in_parent = any(b.bed_id == bed.bed_id for b in (parent.beds if parent else ()))
    return passed(
        moved and resolved,
        f"transfer_ok={moved} current_bed_resolves={resolved} "
        f"parent_ward_lists_room_bed={in_parent}",
    )


async def disposition_discharge_lists_to_discharge(x: AdtCtx) -> Result:
    note = "LT-P2 discharge order" if x.config.concept_map.get("disposition_note") else None
    r = await x.record.write_disposition(
        event_id=uuid4(),
        patient_uuid=x.state["pA"],
        visit_uuid=x.state["ipdA"],
        provider_uuid=x.provider,
        kind="discharge",
        at=datetime.now(UTC),
        note=note,
    )
    x.ours.add(r.encounter_uuid)
    rows = await x.record.adt_worklist("to_discharge")
    listed = any(
        r_.patient_uuid == x.state["pA"] and r_.visit_uuid == x.state["ipdA"] for r_ in rows
    )
    return passed(r.created and listed, f"disposition_created={r.created} listed={listed}")


async def discharge_frees_bed(x: AdtCtx) -> Result:
    a = x.adt(x.state["pA"], None, x.state["ipdA"])
    r = await x.record.discharge(a, end_visit=False)
    x.state["dischA_enc"] = r.encounter_uuid
    x.ours.add(r.encounter_uuid)
    current = await x.record.current_bed(x.state["pA"])
    status, _ = await x.bed_status(x.state["beds"][3])
    churn = await _bed_uuid_churn(x, x.state["beds"][3])
    retry = await x.record.discharge(a, end_visit=False)
    st = await x.visit_state(x.state["ipdA"])
    ok = (
        r.created
        and current is None
        and status == "AVAILABLE"
        and not retry.created
        and retry.encounter_uuid == r.encounter_uuid
        and st["attrs"].get("Admission Status") == "Discharged"
        and st["open"]
    )
    return passed(
        ok,
        f"bed_freed={current is None} bed_status={status} "
        f"marker_obs_saved(retry_found)={not retry.created} "
        f"admission_status={st['attrs'].get('Admission Status')} "
        f"admission_visit_reuse visit_open={st['open']} "
        f"record {churn}",
    )


async def undo_discharge(x: AdtCtx) -> Result:
    got = await x.raw(
        "DELETE",
        f"bahmnicore/bahmniencounter/{x.state['dischA_enc']}",
        params={"reason": "Undo Discharge"},
    )
    st = await x.visit_state(x.state["ipdA"])
    current = await x.record.current_bed(x.state["pA"])
    status = st["attrs"].get("Admission Status")
    return passed(
        got.ok and status == "Admitted" and current is None,
        f"undo={got.status} admission_status={status} bed_empty={current is None} "
        "(as Bahmni's ADT screen: DELETE bahmniencounter)",
    )


async def voided_encounter_visibility(x: AdtCtx) -> Result:
    st = await x.record._adt_visit(x.state["ipdA"])
    row = next(
        (
            e
            for e in st["encounters"]
            if isinstance(e, dict) and e.get("uuid") == x.state["dischA_enc"]
        ),
        None,
    )
    omitted = row is None
    # A new discharge needs a new event id (the voided marker is gone either way).
    again = await x.record.discharge(x.adt(x.state["pA"], None, x.state["ipdA"]), end_visit=False)
    x.state["dischA2_enc"] = again.encounter_uuid
    x.ours.add(again.encounter_uuid)
    return passed(
        omitted or (row is not None and row.get("voided") is True),
        f"passed_by={'omitted' if omitted else 'voided_flag'} voided_discharge_omitted={omitted} "
        f"voided_flag={None if row is None else row.get('voided')} "
        f"redischarge_new_event_created={again.created} "
        "ui_offers_undo_on_stopped_visit=no (Bahmni ADT source: only while the visit is open)",
    )


async def discharge_summary_type(x: AdtCtx) -> Result:
    c = x.config
    ds = c.discharge_summary_encounter_type_uuid
    if not ds:
        return passed(False, "discharge_summary_encounter_type_uuid unset (provisioning missing)")
    got = await x.raw("GET", f"encountertype/{ds}", params={"v": "custom:(name)"})
    name = (got.body or {}).get("name") if isinstance(got.body, dict) else None
    distinct = (
        ds
        not in {
            c.admission_encounter_type_uuid,
            c.transfer_encounter_type_uuid,
            c.discharge_encounter_type_uuid,
        }
        and str(name).upper() not in ADT_TYPE_NAMES
    )
    obs = [RecordObs("vitals_pulse", 72.0)] if "vitals_pulse" in c.concept_map else []
    r = await x.record.write_encounter(
        RecordEncounterInput(
            note_id=uuid4(),
            patient_uuid=x.state["pA"],
            visit_uuid=x.state["ipdA"],
            provider_uuid=x.provider,
            encounter_at=datetime.now(UTC).replace(microsecond=0),
            obs=obs,
            diagnoses=[],
            drug_orders=[],
            encounter_type_uuid=ds,
        )
    )
    x.ours.add(r.encounter_uuid)
    st = await x.record._adt_visit(x.state["ipdA"])
    row = next(
        (e for e in st["encounters"] if isinstance(e, dict) and e.get("uuid") == r.encounter_uuid),
        {},
    )
    typed = (row.get("encounterType") or {}).get("uuid") == ds
    separate = r.encounter_uuid != x.state["dischA2_enc"]
    return passed(
        distinct and typed and separate,
        f"type_name={name} distinct_from_adt={distinct} lands_with_type={typed} "
        f"not_merged_into_discharge={separate}",
    )


async def discharge_visit_still_open_then_end_visit(x: AdtCtx) -> Result:
    visit = x.state["ipdA"]
    open_after_discharge = (await x.visit_state(visit))["open"]
    first = await x.record.end_visit(visit)
    resp = x.last("POST", "/endVisit")
    stop1 = (await x.visit_state(visit))["stop"]
    second = await x.record.end_visit(visit)
    stop2 = (await x.visit_state(visit))["stop"]
    x.state["stopA"] = stop1
    shape = (
        "not called"
        if resp is None
        else f"{resp[0]}:{'dict' if isinstance(resp[1], dict) else type(resp[1]).__name__}"
    )
    return passed(
        open_after_discharge and first and not second and stop1 is not None and stop1 == stop2,
        f"open_after_discharge={open_after_discharge} end_visit={first} again={second} "
        f"stop_unchanged={stop1 == stop2} endVisit_response={shape}",
    )


async def discharge_summary_after_visit_end(x: AdtCtx) -> Result:
    c = x.config
    ds = c.discharge_summary_encounter_type_uuid or None
    stop = when(x.state["stopA"])
    if stop is None:
        return passed(False, "visit stop time unreadable")
    # A visit that stopped on an earlier day: one synthetic patient, a back-dated visit.
    patient = await x.new_patient("PastVisit")
    yesterday = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    past = await x.raw(
        "POST",
        "visit",
        body={
            "patient": patient,
            "visitType": c.opd_visit_type_uuid,
            "location": c.location_uuid,
            "startDatetime": iso(yesterday - timedelta(hours=1)),
            "stopDatetime": iso(yesterday),
        },
    )
    past_uuid = past.body.get("uuid") if isinstance(past.body, dict) else None
    cases = [
        ("now", x.state["pA"], x.state["ipdA"], datetime.now(UTC).replace(microsecond=0)),
        ("stop_same_day", x.state["pA"], x.state["ipdA"], stop),
    ]
    if past_uuid:
        cases.append(("stop_earlier_day", patient, past_uuid, yesterday))
    out = []
    for label, who, visit, at in cases:
        try:
            r = await x.record.write_encounter(
                RecordEncounterInput(
                    note_id=uuid4(),
                    patient_uuid=who,
                    visit_uuid=visit,
                    provider_uuid=x.provider,
                    encounter_at=at,
                    obs=[],
                    diagnoses=[],
                    drug_orders=[],
                    encounter_type_uuid=ds,
                )
            )
            x.ours.add(r.encounter_uuid)
            st = await x.record._adt_visit(visit)
            here = any(
                e.get("uuid") == r.encounter_uuid for e in st["encounters"] if isinstance(e, dict)
            )
            out.append(f"{label}=landed({'same visit' if here else 'another visit'})")
        except HmsAdapterError as exc:
            out.append(f"{label}={type(exc).__name__}")
    if not past_uuid:
        out.append(f"stop_earlier_day=not probed (back-dated visit {past.status})")
    return record(" ".join(out))


async def visit_list_order(x: AdtCtx) -> Result:
    patient = x.state["pA"]
    await asyncio.sleep(1.1)  # visit start times are second-resolution: no ties
    third, _ = await x.record.start_visit(patient)
    got = await x.raw(
        "GET",
        "visit",
        params={
            "patient": patient,
            "includeInactive": "true",
            "limit": "2",
            "v": "custom:(uuid,startDatetime)",
        },
    )
    rows = got.results()
    starts = [str(r.get("startDatetime")) for r in rows]
    newest_first = (
        len(rows) == 2
        and rows[0].get("uuid") == third.uuid
        and starts == sorted(starts, reverse=True)
    )
    return passed(
        newest_first, f"limit=2 of 3 visits: newest_first={newest_first} rows={len(rows)}"
    )


def _admission_body(x: AdtCtx, patient: str, visit: str) -> dict[str, Any]:
    return {
        "patientUuid": patient,
        "visitUuid": visit,
        "encounterTypeUuid": x.config.admission_encounter_type_uuid,
        "visitTypeUuid": x.config.ipd_visit_type_uuid,
        "locationUuid": x.config.location_uuid,
        "providers": [{"uuid": x.provider}],
        "observations": [
            {"concept": {"uuid": x.config.note_marker_concept_uuid}, "value": f"sm:adt:{uuid4()}"}
        ],
    }


async def admission_merge_window(x: AdtCtx) -> Result:
    patient = await x.new_patient("Merge")
    visit, _ = await x.record.start_visit(patient, x.config.ipd_visit_type_uuid)
    first = await x.raw(
        "POST", "bahmnicore/bahmniencounter", body=_admission_body(x, patient, visit.uuid)
    )
    second = await x.raw(
        "POST", "bahmnicore/bahmniencounter", body=_admission_body(x, patient, visit.uuid)
    )
    e1 = first.body.get("encounterUuid") if isinstance(first.body, dict) else None
    e2 = second.body.get("encounterUuid") if isinstance(second.body, dict) else None
    if not (e1 and e2):
        return inconclusive(f"admissions -> {first.status}/{second.status}")
    return record(f"two quick ADMISSIONs (one provider, one visit) merged={e1 == e2}")


async def encounter_match_multiple(x: AdtCtx) -> Result:
    c = x.config
    patient = await x.new_patient("Match")
    visit, _ = await x.record.start_visit(patient)

    def note() -> RecordEncounterInput:
        return RecordEncounterInput(
            note_id=uuid4(),
            patient_uuid=patient,
            visit_uuid=visit.uuid,
            provider_uuid=x.provider,
            encounter_at=datetime.now(UTC).replace(microsecond=0),
            obs=[],
            diagnoses=[],
            drug_orders=[],
        )

    first = await x.record.write_encounter(note())
    roles = await x.raw("GET", "encounterrole", params={"v": "custom:(uuid,display)"})
    role = next(iter(roles.results()), None)
    if role is None:
        return inconclusive("no encounter role to file a second encounter")
    # A second consultation encounter beside the first, filed through core REST (no merge).
    core = await x.raw(
        "POST",
        "encounter",
        body={
            "patient": patient,
            "visit": visit.uuid,
            "encounterType": c.consultation_encounter_type_uuid,
            "location": c.location_uuid,
            "encounterDatetime": iso(datetime.now(UTC)),
            "encounterProviders": [{"provider": x.provider, "encounterRole": role["uuid"]}],
        },
    )
    if not core.ok:
        return inconclusive(f"second encounter via core REST -> {core.status}")
    try:
        third = await x.record.write_encounter(note())
        outcome = (
            "merged into the first"
            if third.encounter_uuid == first.encounter_uuid
            else (
                "merged into the second"
                if third.encounter_uuid == core.body.get("uuid")
                else "new encounter"
            )
        )
    except TransientError as exc:
        outcome = (
            "HTTP 500 (body in samples; Bahmni's 'More than one encounter matches' is a 500)"
            if "HTTP 500" in str(exc)
            else err(exc)
        )
    except HmsAdapterError as exc:
        outcome = type(exc).__name__
    return record(f"third consultation write with two candidates -> {outcome}")


async def admit_conversion_atomic(x: AdtCtx) -> Result:
    patient = await x.new_patient("Atomic")
    visit, _ = await x.record.start_visit(patient)
    body = _admission_body(x, patient, visit.uuid)
    del body["visitUuid"]
    body["observations"].append({"concept": {"uuid": str(uuid4())}, "value": "unknown concept"})
    got = await x.raw(
        "POST",
        "bahmnicore/visit/endVisitAndCreateEncounter",
        params={"visitUuid": visit.uuid},
        body=body,
    )
    if got.ok:
        return inconclusive("the poisoned conversion was accepted; nothing to observe")
    st = await x.visit_state(visit.uuid)
    others = [v for v in await x.open_visits(patient) if v != visit.uuid]
    return record(
        f"failed conversion -> {got.status}; opd_visit_still_open={st['open']} "
        f"(endVisit rolled back={st['open']}) other_open_visits={len(others)}"
    )


async def ipd_side_effects(x: AdtCtx) -> Result:
    got = await x.raw(
        "GET",
        f"visit/{x.state['ipdA']}",
        params={"v": "custom:(uuid,encounters:(uuid,voided,encounterType:(display)))"},
    )
    rows = (
        [e for e in (got.body or {}).get("encounters") or [] if isinstance(e, dict)]
        if isinstance(got.body, dict)
        else []
    )

    def kind(e: dict[str, Any]) -> str:
        return str((e.get("encounterType") or {}).get("display"))

    types = Counter(kind(e) for e in rows)
    foreign = Counter(kind(e) for e in rows if e.get("uuid") not in x.ours)
    st = await x.visit_state(x.state["ipdA"])
    return record(
        f"encounter_types={dict(types)} not_written_by_adt_calls={dict(foreign)} "
        f"visit_attributes={sorted(st['attrs'])}"
    )


async def feed_titles(x: AdtCtx) -> Result:
    got = await x.raw("GET", ENCOUNTER_FEED)
    if not got.ok or not isinstance(got.body, str):
        return passed(False, f"encounter feed -> {got.status}")
    root = ET.fromstring(got.body)
    titles = Counter(
        (e.findtext(f"{ATOM}title") or "").strip() for e in root.findall(f"{ATOM}entry")
    )
    return record(f"recent page titles={dict(titles)}")


# ─── box-wide state: last, opt-in, restored in finally ───────────────────────


def _needs_global(x: AdtCtx) -> str | None:
    if not x.global_ok:
        return f"changes box-wide state; set {GLOBAL_ENV}=1 to run"
    if x.admin_http is None:
        return "box-wide probes run as the admin; set BAHMNI_ADMIN_USER/BAHMNI_ADMIN_PASS"
    if x.restore_path is None:
        return "set HARNESS_SAMPLES_DIR: the restore file is written there first"
    if x.restore_failed:
        return "an earlier box-wide change was not restored; no further box-wide probes"
    if x.cancelled:
        return "interrupted by a signal; no further box-wide probes"
    return None


def _save_restore(x: AdtCtx) -> None:
    """Write every box-wide change's original to the local restore file (0600)."""
    assert x.restore_path is not None
    fd = os.open(x.restore_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(x.restore_entries, f, indent=2, default=str)
    os.chmod(x.restore_path, 0o600)


def _remember(x: AdtCtx, entry: dict[str, Any]) -> dict[str, Any]:
    """Record the original BEFORE changing it; the restore file outlives the process."""
    entry["restored"] = False
    x.restore_entries.append(entry)
    _save_restore(x)
    print(f"BOXSTATE changed {entry['what']}; restore file {x.restore_path}", flush=True)
    return entry


def _manual(entry: dict[str, Any]) -> str:
    base = "$BAHMNI_BASE_URL/openmrs/ws/rest/v1"
    if entry["kind"] == "gp":
        return (
            f"POST {base}/systemsetting/{entry['uuid']} as the admin with body "
            f'{{"value": <the "original" of {entry["property"]} in the restore file>}}'
        )
    return (
        f"POST {base}/locationtag/{entry['uuid']} as the admin with body "
        f'{{"name": "{entry["original"]}"}}'
    )


async def _restore(x: AdtCtx, entry: dict[str, Any]) -> bool:
    """Put one box-wide change back, verified by a read-back; two tries; loud on failure.

    A cancellation caught here (a signal mid-restore) does not stop the retry, but it is
    re-raised once the restore is settled, so the probe and the run unwind.
    """
    cancelled = False
    try:
        return await _restore_tries(x, entry)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if cancelled:
            x.cancelled = True


async def _restore_tries(x: AdtCtx, entry: dict[str, Any]) -> bool:
    last = ""
    interrupted: BaseException | None = None
    for _ in (1, 2):
        try:
            if entry["kind"] == "gp":
                await x.raw(
                    "POST",
                    f"systemsetting/{entry['uuid']}",
                    body={"value": entry["original"]},
                    admin=True,
                )
                ok = (await x.gp(entry["property"], admin=True))[1] == entry["original"]
            else:
                await x.raw(
                    "POST",
                    f"locationtag/{entry['uuid']}",
                    body={"name": entry["original"]},
                    admin=True,
                )
                got = await x.raw(
                    "GET", f"locationtag/{entry['uuid']}", params={"v": "custom:(name)"}, admin=True
                )
                ok = isinstance(got.body, dict) and got.body.get("name") == entry["original"]
            if ok:
                entry["restored"] = True
                _save_restore(x)
                if interrupted is not None:
                    raise interrupted
                return True
            last = "read-back differs"
        except asyncio.CancelledError as exc:
            if interrupted is not None:
                raise  # cancelled again after a successful restore: unwind
            interrupted, last = exc, "CancelledError"
        except Exception as exc:  # noqa: BLE001 - a restore must report
            last = type(exc).__name__
    x.restore_failed.append(entry)
    # Operator line, deliberately not scrubbed: it names what to put back by hand.
    print(
        f"RESTORE FAILED {entry['kind']} {entry['what']} ({last}): manual: {_manual(entry)}; "
        f"restore file {x.restore_path}",
        flush=True,
    )
    if interrupted is not None:
        raise interrupted
    return False


async def _worklist_500(x: AdtCtx, location: str, label: str) -> Raw:
    got = await x.raw(
        "GET",
        "bahmnicore/sql",
        params={"q": WORKLIST_GPS[0], "location_uuid": location, "v": "full"},
    )
    x.sample(f"worklist_{label}", got.body)
    return got


async def worklist_misconfig(x: AdtCtx) -> Result:
    out, ok = [], True
    for label, location in (("unknown_uuid", str(uuid4())), ("untagged", x.state.get("untagged"))):
        if not location:
            out.append(
                f"{label}=not probed (no untagged location: run ward_layout_on_untagged_location)"
            )
            ok = False
            continue
        try:
            rows = await x.client(visit_location_uuid=location).adt_worklist("to_admit")
            out.append(f"{label}=list[{len(rows)}]")
            ok = False
        except TransientError:
            got = await _worklist_500(x, location, label)
            out.append(f"{label}={got.shape()}")
            ok = ok and got.status >= 500
    out.append("other_visit_location=not probed (one visit location on the box)")
    reason = _needs_global(x)
    if reason:
        return passed(ok, " ".join(out) + f" unset_gp=SKIPPED ({reason})")
    uuid, original = await x.gp(WORKLIST_GPS[0], admin=True)
    if uuid is None or original is None:
        return inconclusive(" ".join(out) + " unset_gp=GP absent, nothing to unset")
    entry = _remember(
        x,
        {
            "kind": "gp",
            "what": f"GP {WORKLIST_GPS[0]} blanked",
            "uuid": uuid,
            "property": WORKLIST_GPS[0],
            "original": original,
        },
    )
    t0 = time.monotonic()
    unset_500 = False
    try:
        changed = await x.raw("POST", f"systemsetting/{uuid}", body={"value": ""}, admin=True)
        if not changed.ok:
            out.append(f"unset_gp=cannot change GP ({changed.status})")
        else:
            got = await _worklist_500(x, x.config.visit_location_uuid, "unset_gp")
            unset_500 = got.status >= 500
            out.append(f"unset_gp={got.shape()}")
    finally:
        restored = await _restore(x, entry)
        out.append(f"gp_restored={restored} changed_for_s={time.monotonic() - t0:.1f}")
    if not restored:
        return "FAIL", " ".join(out) + " BOX STATE NOT RESTORED"
    return passed(ok and unset_500, " ".join(out))


async def ward_admission_tag_missing(x: AdtCtx) -> Result:
    tags = await x.raw("GET", "locationtag", params={"q": ADMISSION_TAG, "v": "custom:(uuid,name)"})
    tag = next(
        (
            t
            for t in tags.results()
            if str(t.get("name", "")).casefold() == ADMISSION_TAG.casefold()
        ),
        None,
    )
    parent = await x.raw(
        "GET", f"location/{x.state['ward']}", params={"v": "custom:(uuid,parentLocation:(uuid))"}
    )
    parent_ok = (
        isinstance(parent.body, dict)
        and (parent.body.get("parentLocation") or {}).get("uuid") == x.config.visit_location_uuid
    )
    out = [f"tag_rep_ok={tag is not None}", f"parent_rep_ok={parent_ok}"]
    if tag is None or not parent_ok:
        return "FAIL", " ".join(out)
    reason = _needs_global(x)
    if reason:
        return "SKIP", " ".join(out) + f" create_without_tag=SKIPPED ({reason})"
    entry = _remember(
        x,
        {
            "kind": "tag",
            "what": f"location tag '{tag['name']}' renamed",
            "uuid": tag["uuid"],
            "original": tag["name"],
        },
    )
    t0 = time.monotonic()
    client, raw_status = "not run", 0
    try:
        renamed = await x.raw(
            "POST",
            f"locationtag/{tag['uuid']}",
            body={"name": f"{ADMISSION_TAG} {NAME}"},
            admin=True,
        )
        if not renamed.ok:
            out.append(f"cannot rename the tag ({renamed.status})")
        else:
            try:
                made = await x.record.create_ward(
                    name=f"{NAME} NoTag",
                    parent_location_uuid=x.config.visit_location_uuid,
                    rows=1,
                    columns=1,
                )
                client = "not refused"
                x.later(3, "ward", f"{NAME} NoTag", lambda: x._delete(f"location/{made.uuid}"))
            except HmsAdapterError as exc:
                client = type(exc).__name__
            got = await x.raw(
                "POST",
                "admissionLocation",
                body={
                    "name": f"{NAME} NoTagRaw",
                    "parentLocationUuid": x.config.visit_location_uuid,
                },
            )
            raw_status = got.status
            ward = (got.body.get("ward") or {}) if isinstance(got.body, dict) else {}
            if got.ok and ward.get("uuid"):
                x.later(
                    3, "ward", f"{NAME} NoTagRaw", lambda: x._delete(f"location/{ward['uuid']}")
                )
            out += [f"client_precheck={client}", f"raw_create_without_tag={raw_status}"]
    finally:
        restored = await _restore(x, entry)
        out.append(f"tag_restored={restored} changed_for_s={time.monotonic() - t0:.1f}")
    if not restored:
        return "FAIL", " ".join(out) + " BOX STATE NOT RESTORED"
    if client == "not run":
        return inconclusive(" ".join(out))
    return passed(client == "ConfigError" and raw_status >= 500, " ".join(out))


async def admitted_visit_survives_stale_close(x: AdtCtx) -> Result:
    reason = _needs_global(x)
    if reason is None and os.environ.get(STALE_ENV) != "1":
        reason = f"irreversible: also set {STALE_ENV}=1"
    if reason:
        return "SKIP", f"{reason} (it stops EVERY idle non-admitted visit on the box, for good)"
    tasks = await x.raw(
        "GET", "taskdefinition", params={"v": "custom:(uuid,name,taskClass)"}, admin=True
    )
    task = next(
        (
            t
            for t in tasks.results()
            if str(t.get("taskClass", "")).endswith("CloseStaleVisitsTask")
        ),
        None,
    )
    if task is None:
        return inconclusive(f"no CloseStaleVisitsTask definition ({tasks.status})")
    c = x.config

    async def visit_at_visit_location(patient: str, visit_type: str) -> str | None:
        got = await x.raw(
            "POST",
            "visit",
            body={"patient": patient, "visitType": visit_type, "location": c.visit_location_uuid},
        )
        return got.body.get("uuid") if isinstance(got.body, dict) else None

    # The task scans visits at "Visit Location"-tagged locations only: the admitted visit
    # and the control sit there, as Bahmni's own visits (and sm_common's) do. A second
    # control is a visit at the login location, a child of the Visit Location.
    admitted = await x.new_patient("Stale")
    ipd_visit = await visit_at_visit_location(admitted, c.ipd_visit_type_uuid)
    control = await x.new_patient("StaleCtl")
    ctl_visit = await visit_at_visit_location(control, c.opd_visit_type_uuid)
    if not ipd_visit or not ctl_visit:
        return inconclusive("could not start visits at the visit location")
    ours = {b.bed_id for b in x.state["beds"]}
    ward = await x.record.get_ward(x.state["ward"])
    free = next(
        (
            b
            for b in (ward.beds if ward else ())
            if b.bed_id in ours and b.status == "AVAILABLE" and not b.patient_uuids
        ),
        None,
    )
    if free is None:
        return inconclusive("no free bed in this run's ward for the admitted visit")
    r = await x.record.admit(x.adt(admitted, free, ipd_visit))
    login_ctl = await x.new_patient("StaleLogin")
    login_got = await x.raw(
        "POST",
        "visit",
        body={
            "patient": login_ctl,
            "visitType": c.opd_visit_type_uuid,
            "location": c.location_uuid,
        },
    )
    login_uuid = login_got.body.get("uuid") if isinstance(login_got.body, dict) else None
    if not login_uuid:
        return inconclusive(
            f"could not start the login-location control visit ({login_got.status})"
        )
    uuid, original = await x.gp("emrapi.visitExpireHours", admin=True)
    if uuid is None:
        return inconclusive("emrapi.visitExpireHours GP absent; not creating one")
    print(
        "BOXSTATE warning: the stale-visit task now stops every idle non-admitted visit "
        "at a Visit Location on this box",
        flush=True,
    )
    entry = _remember(
        x,
        {
            "kind": "gp",
            "what": "GP emrapi.visitExpireHours set to 0",
            "uuid": uuid,
            "property": "emrapi.visitExpireHours",
            "original": original,  # None stays None
        },
    )
    t0 = time.monotonic()
    ran_status, closed = 0, False
    try:
        changed = await x.raw("POST", f"systemsetting/{uuid}", body={"value": "0"}, admin=True)
        if changed.ok:
            ran = await x.raw(
                "POST",
                "taskaction",
                body={"action": "RUNTASK", "tasks": [task["name"]]},
                admin=True,
                timeout_s=STALE_TASK_TIMEOUT_S,
            )
            ran_status = ran.status
            for _ in range(STALE_POLL_S):
                closed = not (await x.visit_state(ctl_visit))["open"]
                if closed:
                    break
                await asyncio.sleep(1)
    finally:
        restored = await _restore(x, entry)
        window = time.monotonic() - t0
    survives = (await x.visit_state(r.visit_uuid))["open"]
    login_closed = not (await x.visit_state(login_uuid))["open"]
    detail = (
        f"task_run={ran_status} control_visit_closed={closed} admitted_visit_open={survives} "
        f"login_location_visit_closed={login_closed} "
        f"gp_restored={restored} changed_for_s={window:.1f}"
    )
    if not restored:
        return "FAIL", detail + " BOX STATE NOT RESTORED"
    if not closed:
        return inconclusive(detail + " (the task did not close the control visit)")
    return passed(survives, detail)


# Run order = shared-box safety: read-only, own master data, own patients, box-wide last.
ADT_CHECKS: list[tuple[str, str, AdtCheck]] = [
    ("module_versions", "stack_module_versions", module_versions),
    (
        "adt_config_resolved",
        "adt_config_keys,disposition_mappings,worklist_queries",
        adt_config_resolved,
    ),
    ("disposition_mappings", "disposition_mappings", disposition_mappings),
    ("type_names_exact", "encounter_type_names", type_names_exact),
    ("emr_gps_set", "inpatient_visit_settings", emr_gps_set),
    ("unknown_ward_uuid", "unknown_ward_uuid", unknown_ward_uuid),
    ("ward_create", "ward_bed_layout", ward_create),
    ("ward_layout", "ward_bed_layout", ward_layout),
    ("ward_create_response_shape", "ward_create_response_shape", ward_create_response_shape),
    ("bed_create_response_shape", "bed_number_max_len", bed_create_response_shape),
    ("ward_layout_shrink", "ward_layout_shrink", ward_layout_shrink),
    (
        "ward_layout_on_untagged_location",
        "ward_untagged_location",
        ward_layout_on_untagged_location,
    ),
    ("bed_create_orphan_on_cell_conflict", "bed_create_orphan", bed_create_orphan_on_cell_conflict),
    ("bedtype_name_case", "bedtype_name_case", bedtype_name_case),
    (
        "location_duplicate_retired_first",
        "location_duplicate_retired",
        location_duplicate_retired_first,
    ),
    ("ward_location_search_rep", "ward_location_search_rep", ward_location_search_rep),
    ("ward_duplicate_name_400_shape", "ward_duplicate_name_refusal", ward_duplicate_name_400_shape),
    # Last of the master data: a case-sensitive server would create a same-name ward.
    ("ward_name_case", "ward_name_case", ward_name_case),
    ("find_by_marker_live", "find_by_marker", find_by_marker_live),
    ("disposition_admit_lists_to_admit", "admit_worklist", disposition_admit_lists_to_admit),
    ("admit_new_ipd_visit", "admit_encounter", admit_new_ipd_visit),
    ("admit_retry_same_encounter", "admit_encounter", admit_retry_same_encounter),
    ("bed_occupied_after_admit", "bed_single_occupancy", bed_occupied_after_admit),
    ("adt_visit_rep_parses", "adt_visit_rep", adt_visit_rep_parses),
    ("visit_uuid_kept", "admission_visit_reuse", visit_uuid_kept),
    (
        "second_patient_same_bed_refused_by_client",
        "bed_single_occupancy",
        second_patient_same_bed_refused_by_client,
    ),
    ("bed_read_shape", "bed_read_by_id", bed_read_shape),
    ("bed_assign_response_shape", "bed_assign_response_shape", bed_assign_response_shape),
    ("bed_assign_unknown_id_rollback", "bed_assign_rollback", bed_assign_unknown_id_rollback),
    ("transfer_moves_bed", "transfer_bed_move", transfer_moves_bed),
    ("transfer_merge_window", "transfer_bed_move", transfer_merge_window),
    ("child_location_bed_current_bed", "child_location_bed", child_location_bed_current_bed),
    (
        "disposition_discharge_lists_to_discharge",
        "discharge_flow",
        disposition_discharge_lists_to_discharge,
    ),
    ("discharge_frees_bed", "discharge_flow", discharge_frees_bed),
    ("undo_discharge", "undo_discharge", undo_discharge),
    ("voided_encounter_visibility", "voided_encounter_visibility", voided_encounter_visibility),
    ("discharge_summary_type", "encounter_type_names", discharge_summary_type),
    (
        "discharge_visit_still_open_then_end_visit",
        "discharge_flow",
        discharge_visit_still_open_then_end_visit,
    ),
    (
        "discharge_summary_after_visit_end",
        "discharge_summary_after_visit_end",
        discharge_summary_after_visit_end,
    ),
    ("visit_list_order", "visit_list_order", visit_list_order),
    ("admission_merge_window", "admission_merge_window", admission_merge_window),
    ("encounter_match_multiple", "encounter_match_multiple", encounter_match_multiple),
    ("admit_conversion_atomic", "admit_conversion_atomic", admit_conversion_atomic),
    ("ipd_side_effects", "ipd_side_effects", ipd_side_effects),
    ("feed_titles", "encounter_feed_titles", feed_titles),
    ("worklist_misconfig", "worklist_misconfig", worklist_misconfig),
    ("ward_admission_tag_missing", "ward_admission_tag", ward_admission_tag_missing),
    (
        "admitted_visit_survives_stale_close",
        "inpatient_visit_settings",
        admitted_visit_survives_stale_close,
    ),
]
# A failure here stops the run (stack drift, or nothing to drive the client with).
GATES = {"module_versions", "adt_config_resolved"}
PRIVILEGE = re.compile(r"Privileges? required:?\s*\[?([A-Za-z :,]+)")


def _missing_privilege(x: AdtCtx) -> str:
    """The privilege Bahmni named in its last 403, if it named one."""
    for _m, _p, status, body in reversed(x.tap):
        if status == 403:
            hit = PRIVILEGE.search(json.dumps(body) if body is not None else "")
            return hit.group(1).strip() if hit else "not named by Bahmni"
    return "not named by Bahmni"


async def _one(x: AdtCtx, name: str, fn: AdtCheck) -> Result:
    x.transport_error = False
    try:
        return await fn(x)
    except AuthError as exc:
        x.sample(f"error_{name}", {"type": type(exc).__name__, "message": str(exc)})
        return inconclusive(f"HTTP 403: missing privilege ({_missing_privilege(x)})")
    except KeyError as exc:
        # An earlier check this one builds on did not complete: blocked, not disproved.
        return inconclusive(f"blocked: prerequisite {exc} missing (an earlier check failed)")
    except Exception as exc:  # noqa: BLE001 - every failure is a reported FAIL
        # The message may carry server text: samples only, never the CHECK line.
        x.sample(f"error_{name}", {"type": type(exc).__name__, "message": str(exc)})
        x.transport_error = isinstance(exc, TransientError) and "transport" in str(exc)
        return "FAIL", err(exc)


MANUAL_CLEANUP = {
    "patient": "unassign its bed, end its open visits, then void the patient",
    "bed": "DELETE bed/<uuid> (find it by bed number)",
    "room": "retire the location",
    "ward": "retire the location",
    "location": "retire the location",
    "bed type": "DELETE bedtype/<uuid> (only if no bed uses it)",
}


async def _cleanup(x: AdtCtx, name: str = NAME) -> tuple[int, int]:
    """Run the cleanups; after 3 transport errors in a row, list what is LEFT by hand."""
    done = 0
    order = sorted(enumerate(x.cleanups), key=lambda p: (p[1].priority, -p[0]))
    streak = 0
    for pos, (_, item) in enumerate(order):
        if streak >= 3:
            left = [i for _, i in order[pos:]]
            print(
                f"CLEANUP ABORTED tunnel down: {len(left)} left; find them by the name {name} "
                "and clean them by hand as the admin:",
                flush=True,
            )
            for i in left:
                print(f"  LEFT {i.kind} {i.label}: {MANUAL_CLEANUP.get(i.kind, 'remove it')}")
            break
        try:
            why = await item.fn()
            streak = 0
        except Exception as exc:  # noqa: BLE001 - cleanup reports, never raises
            why = err(exc)
            transport = isinstance(exc, TransientError) and "transport" in str(exc)
            streak = streak + 1 if transport else 0
        skipped = why is not None and why.startswith("SKIPPED")
        if why is None or skipped:
            done += 1
        verdict = "OK" if why is None else (why if skipped else f"FAILED {why}")
        print(scrub(f"CLEANUP {item.kind} {item.label} {verdict}"), flush=True)
    return done, len(x.cleanups)


def _signals(x: AdtCtx, task: asyncio.Task[Any] | None, during_cleanup: bool) -> None:
    """SIGINT/SIGHUP/SIGTERM cancel the run like Ctrl-C, so restores and cleanup run.

    While cleanup runs, they only print: cleanup must finish.
    """
    loop = asyncio.get_running_loop()

    def on_signal(signame: str) -> None:
        if during_cleanup:
            print(f"{signame}: cleanup in progress; ignoring (kill -9 to abort)", flush=True)
            return
        x.cancelled = True
        print(f"{signame}: stopping after restores; cleanup follows", flush=True)
        if task is not None:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, on_signal, sig.name)


async def run(
    cfg: dict[str, Any],
    http: BahmniHttp,
    config: BahmniConfig,
    admin_http: BahmniHttp | None = None,
) -> int:
    x = AdtCtx(cfg, http, config, admin_http)
    only = {n for n in os.environ.get("HARNESS_ONLY", "").split(",") if n}
    checks = [(n, topic, f) for n, topic, f in ADT_CHECKS if not only or n in only]
    counts: Counter[str] = Counter()
    t0 = time.monotonic()
    deadline = t0 + float(os.environ.get("HARNESS_DEADLINE_S", "1200"))
    print(
        f"ADT RUN {NAME} (synthetic records carry this name) global_state={x.global_ok} "
        f"admin_login={'set' if admin_http else 'unset'}",
        flush=True,
    )
    stopped = ""
    transport_streak = 0
    _signals(x, asyncio.current_task(), during_cleanup=False)
    try:
        for name, topic, fn in checks:
            if x.cancelled:
                stopped = "interrupted by a signal"
                break
            if time.monotonic() > deadline:
                stopped = "deadline (HARNESS_DEADLINE_S) reached"
                break
            status, detail = await _one(x, name, fn)
            counts[status] += 1
            print(scrub(f"CHECK {name} {status} {topic} {detail}"), flush=True)
            transport_streak = transport_streak + 1 if x.transport_error else 0
            if transport_streak >= 3:
                stopped = "3 checks in a row hit transport errors (tunnel down?)"
                break
            if (
                name in GATES
                and status != "PASS"
                and os.environ.get("HARNESS_CONTINUE_AFTER_GATE") != "1"
            ):
                stopped = f"gate {name} did not pass (HARNESS_CONTINUE_AFTER_GATE=1 to go on)"
                break
    except asyncio.CancelledError:
        stopped = "interrupted by a signal"
    finally:
        if stopped:
            print(f"STOPPED: {stopped}; remaining checks not run", flush=True)
        _signals(x, None, during_cleanup=True)
        done, total = await _cleanup(x)
        if done < total:
            print(
                f"CLEANUP INCOMPLETE {total - done} left; find them by the name {NAME} "
                "(records whose create landed without a readable answer are not tracked: "
                "search that name too)",
                flush=True,
            )
        print(
            f"SUMMARY adt pass={counts['PASS']} fail={counts['FAIL']} "
            f"inconclusive={counts['INCONCLUSIVE']} skip={counts['SKIP']} of={len(checks)} "
            f"cleanup={done}/{total} http_calls={x.calls} writes={x.writes} "
            f"elapsed_s={time.monotonic() - t0:.0f}",
            flush=True,
        )
        if x.restore_failed:
            print(
                f"!!! BOX STATE NOT RESTORED: {len(x.restore_failed)} change(s); follow the "
                f"RESTORE FAILED lines above (restore file {x.restore_path})",
                flush=True,
            )
    if x.restore_failed:
        return 3
    if done < total:
        return 4
    if x.cancelled or stopped:
        return 130 if x.cancelled else 1
    return 0 if counts["FAIL"] == 0 and counts["INCONCLUSIVE"] == 0 else 1
