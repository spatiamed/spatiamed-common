"""Lab result checks: drive ``BahmniLabResultsClient`` against a live Bahmni.

One row per check, ``(name, topic, (status, detail))``, with the statuses of the ADT group
(``adt_checks.py``): PASS, FAIL, INCONCLUSIVE (blocked or undecidable), record-only checks
pass once they have their value. The caller prints the rows (``scrub``-ed details) and
runs the shared cleanup (``adt_checks._cleanup``) after ``run_checks`` returns; every
result group a check writes is registered there and voided before its patient is dropped.

Synthetic data only: patients come from ``AdtCtx.new_patient`` (the run's ``LT-`` name
plus the role ``LT-P4-Lab``); no seed or real patient is touched. The checks never call
the lab system's own write routes: lab orders are placed with a raw ``bahmniencounter``
POST (detail says "order via raw"), results go through the client under test.

Inputs (``cfg["harness"]``, read through the ctx):
- ``lab.numeric_test_concept_uuid``: an orderable Numeric lab test.
- ``lab.panel_concept_uuid``: an orderable panel with at least two member tests.
- ``lab.lab_order_type_uuid`` (optional): else the stock order type named "Lab Order".
- ``lab_results`` (optional): the tenant's lab result mapping; when given, its uuids must
  equal the ones resolved by stock name.
- ``lab_collect`` (optional): ``{patient_uuid, visit_uuid, order_uuid,
  test_concept_uuid}`` of a synthetic patient's order that went through the lab system's
  collection step; without it the no-clobber check is INCONCLUSIVE.
The login location (``BahmniConfig.location_uuid``) is the result location and
``consultation_encounter_type_uuid`` the order encounter type.

Resolved uuids (for the tenant checklist) go to ``HARNESS_SAMPLES_DIR`` only, never into
a detail line.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from adt_checks import (
    AdtCtx,
    Result,
    _one,
    err,
    inconclusive,
    passed,
    record,
    scrub,
)

from sm_common.integrations.adapters.bahmni_http import to_epoch_ms
from sm_common.integrations.adapters.bahmni_lab_results import (
    RECORD_RESULT_NEWER,
    BahmniLabResultsClient,
    LabResultConflictError,
    LabResultsConfig,
)
from sm_common.integrations.exceptions import NotFound
from sm_common.integrations.lab_results_types import (
    LabResultRow,
    LabResultWrite,
    LabResultWriteOutcome,
    LabTestDefinition,
)

ROLE = "LT-P4-Lab"
NOTE = "LT-P4 synthetic result"
VOID_REASON = "LT-P4 harness cleanup"
# Bahmni's lab feed polls about every 15 s; the no-clobber check waits two cycles.
FEED_WAIT_S = 30.0
# The guard write is this much older than the first result: far outside the one-second
# window the client treats as a retry of the same write.
GUARD_OFFSET = timedelta(hours=1)
ENCOUNTER_ROUTE = "bahmnicore/bahmniencounter"
STOCK_ENCOUNTER_TYPE = "LAB_RESULT"
LAB_ORDER_TYPE_NAME = "Lab Order"
# LabResultsConfig key -> (stock concept name, expected datatype).
STOCK_CONCEPTS = {
    "concept_lab_abnormal_uuid": ("LAB_ABNORMAL", "Boolean"),
    "concept_lab_minnormal_uuid": ("LAB_MINNORMAL", "Numeric"),
    "concept_lab_maxnormal_uuid": ("LAB_MAXNORMAL", "Numeric"),
    "concept_lab_notes_uuid": ("LAB_NOTES", "Text"),
    "concept_referred_out_uuid": ("REFERRED_OUT", "Boolean"),
}
GATE = "lab_result_types_resolved"

CheckResult = tuple[str, str, Result]
LabCheck = Callable[[AdtCtx], Awaitable[Result]]


class PrereqMissingError(Exception):
    """A harness input or an earlier check's state is missing: INCONCLUSIVE, not FAIL."""


# ─── plumbing ───────────────────────────────────────────────────────────────


def _lab(x: AdtCtx) -> dict[str, Any]:
    lab: dict[str, Any] = x.state.setdefault("lab", {})
    return lab


def _cfg(x: AdtCtx) -> LabResultsConfig:
    cfg = _lab(x).get("config")
    if not isinstance(cfg, LabResultsConfig):
        raise PrereqMissingError(f"blocked: {GATE} did not resolve the lab result types")
    return cfg


def _client(x: AdtCtx, admin: bool = False) -> BahmniLabResultsClient:
    http = x.admin_http if admin and x.admin_http is not None else x.http
    return BahmniLabResultsClient(http, _cfg(x))


def _input(x: AdtCtx, key: str) -> str:
    lab = x.h.get("lab")
    value = lab.get(key) if isinstance(lab, Mapping) else None
    if not isinstance(value, str) or not value:
        raise PrereqMissingError(f"harness input lab.{key} not set")
    return value


def _state(x: AdtCtx, key: str) -> Any:
    value = _lab(x).get(key)
    if value is None:
        raise PrereqMissingError(f"blocked: prerequisite {key} missing (an earlier check failed)")
    return value


def _now() -> datetime:
    # Whole milliseconds: the record keeps no finer time.
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


async def _named(x: AdtCtx, resource: str, name: str, rep: str) -> dict[str, Any] | None:
    """The one row of ``resource`` whose name is exactly ``name`` (case-insensitive)."""
    got = await x.raw("GET", resource, params={"q": name, "v": rep})
    want = name.casefold()
    hits = []
    for row in got.results():
        names = {str(row.get("display") or "").casefold(), str(row.get("name") or "").casefold()}
        inner = row.get("name")
        if isinstance(inner, dict):
            names.add(str(inner.get("name") or "").casefold())
        if want in names:
            hits.append(row)
    return hits[0] if len(hits) == 1 else None


def _later_void(x: AdtCtx, outcome: LabResultWriteOutcome) -> None:
    """Register every group this run wrote for a void at cleanup (before the patient)."""
    for order, groups in outcome.obs_uuids_by_order.items():
        for obs in groups:

            async def void(obs: str = obs) -> str | None:
                try:
                    await _client(x, admin=True).void_lab_result(obs, VOID_REASON)
                except NotFound:
                    return None
                return None

            x.later(0, "obs", f"{ROLE} result group of order {order}", void)


async def _write(
    x: AdtCtx,
    *,
    patient: str,
    visit: str,
    results: Sequence[LabResultWrite],
    named: bool = True,
) -> LabResultWriteOutcome:
    cfg = _cfg(x)
    outcome = await _client(x).write_lab_results(
        patient_uuid=patient,
        visit_uuid=visit,
        provider_uuid=x.provider,
        location_uuid=cfg.result_location_uuid,
        encounter_datetime=_now(),
        results=results,
        encounter_uuid=str(uuid4()) if named else None,
    )
    _later_void(x, outcome)
    return outcome


async def _definition(x: AdtCtx, concept: str) -> LabTestDefinition:
    return (await _client(x).read_test_definitions([concept]))[0]


def _value(defn: LabTestDefinition, numeric: Decimal) -> tuple[str, Decimal | str]:
    if defn.datatype == "Numeric":
        return "numeric", numeric
    if defn.datatype == "Text":
        return "text", NOTE
    if defn.datatype == "Coded" and defn.answers:
        return "coded", defn.answers[0][0]
    raise PrereqMissingError(f"test datatype {defn.datatype} not writable by the harness")


def _write_for(
    order: str,
    defn: LabTestDefinition,
    at: datetime,
    numeric: Decimal,
    panel: str | None = None,
    *,
    ranges: bool = False,
    abnormal: bool = False,
) -> LabResultWrite:
    kind, value = _value(defn, numeric)
    with_ranges = ranges and kind == "numeric"
    return LabResultWrite(
        order_uuid=order,
        test_concept_uuid=defn.concept_uuid,
        panel_concept_uuid=panel,
        value_kind=kind,  # type: ignore[arg-type]
        value=value,
        min_normal=Decimal("1") if with_ranges else None,
        max_normal=Decimal("10") if with_ranges else None,
        abnormal=abnormal,
        notes=NOTE,
        result_at=at,
    )


def _row(rows: Sequence[LabResultRow], order: str, test: str | None = None) -> LabResultRow | None:
    for r in rows:
        if r.order_uuid == order and (test is None or r.test_uuid == test):
            return r
    return None


def _num(text: str | None) -> Decimal | None:
    try:
        return Decimal(text) if text is not None else None
    except InvalidOperation:
        return None


async def _rows(x: AdtCtx, patient: str, visit: str) -> list[LabResultRow]:
    return await _client(x).read_lab_results(patient, visit_uuids=[visit])


async def _order_type(x: AdtCtx) -> str:
    lab = x.h.get("lab")
    given = lab.get("lab_order_type_uuid") if isinstance(lab, Mapping) else None
    if isinstance(given, str) and given:
        return given
    got = await x.raw("GET", "ordertype", params={"v": "custom:(uuid,name)"})
    for row in got.results():
        if str(row.get("name") or "").casefold() == LAB_ORDER_TYPE_NAME.casefold():
            return str(row["uuid"])
    raise PrereqMissingError(
        f"order type {LAB_ORDER_TYPE_NAME!r} not found and lab.lab_order_type_uuid unset"
    )


async def _patient_visit(x: AdtCtx) -> tuple[str, str]:
    patient = await x.new_patient(ROLE)
    visit, _ = await x.record.start_visit(patient)
    return patient, visit.uuid


async def _place_order(x: AdtCtx, patient: str, visit: str, concept: str) -> str:
    """Place one lab order with a raw encounter POST; its order uuid."""
    encounter_type = x.config.consultation_encounter_type_uuid
    if not encounter_type:
        raise PrereqMissingError("consultation_encounter_type_uuid not configured")
    order_type = await _order_type(x)
    vt = await x.raw("GET", f"visit/{visit}", params={"v": "custom:(uuid,visitType:(display))"})
    vtype = (vt.body or {}).get("visitType") if isinstance(vt.body, dict) else None
    body = {
        "patientUuid": patient,
        "visitUuid": visit,
        "visitType": vtype.get("display") if isinstance(vtype, dict) else None,
        "encounterUuid": str(uuid4()),
        "encounterTypeUuid": encounter_type,
        "locationUuid": x.config.location_uuid,
        "providers": [{"uuid": x.provider}],
        "encounterDateTime": to_epoch_ms(datetime.now(UTC)),
        "orders": [
            {
                "concept": {"uuid": concept},
                "orderTypeUuid": order_type,
                "action": "NEW",
                "careSetting": "OUTPATIENT",
            }
        ],
    }
    got = await x.raw("POST", ENCOUNTER_ROUTE, body=body)
    x.sample(f"lab_order_post_{got.status}", {"shape": got.shape()})
    if not got.ok:
        raise RuntimeError(f"lab order POST HTTP {got.status}")
    for o in (got.body or {}).get("orders") or [] if isinstance(got.body, dict) else []:
        c = o.get("concept") if isinstance(o, dict) else None
        if isinstance(c, dict) and c.get("uuid") == concept and o.get("uuid"):
            return str(o["uuid"])
    # Fallback: the pending row the read lists for that concept.
    for r in await _rows(x, patient, visit):
        if r.status == "pending" and concept in (r.test_uuid, r.panel_uuid):
            return r.order_uuid
    raise RuntimeError("lab order placed but not found in the response or the read")


async def _panel_members(x: AdtCtx, panel: str) -> list[str]:
    got = await x.raw("GET", f"concept/{panel}", params={"v": "custom:(uuid,setMembers:(uuid))"})
    members = (got.body or {}).get("setMembers") if isinstance(got.body, dict) else None
    uuids = [str(m["uuid"]) for m in members or [] if isinstance(m, dict) and m.get("uuid")]
    if len(uuids) < 2:
        raise PrereqMissingError("lab.panel_concept_uuid has fewer than two member tests")
    return uuids[:2]


# ─── checks ─────────────────────────────────────────────────────────────────


async def lab_result_types_resolved(x: AdtCtx) -> Result:
    enc = await _named(x, "encountertype", STOCK_ENCOUNTER_TYPE, "custom:(uuid,name,display)")
    resolved: dict[str, str] = {}
    datatypes: dict[str, str | None] = {}
    bad: list[str] = []
    if enc is None:
        bad.append(STOCK_ENCOUNTER_TYPE)
    else:
        resolved["lab_result_encounter_type_uuid"] = str(enc["uuid"])
    for key, (name, want) in STOCK_CONCEPTS.items():
        row = await _named(x, "concept", name, "custom:(uuid,display,name:(name),datatype:(name))")
        if row is None:
            bad.append(name)
            continue
        dt = row.get("datatype")
        got = str(dt.get("name")) if isinstance(dt, dict) else None
        datatypes[name] = got
        if got != want:
            bad.append(f"{name} datatype {got} want {want}")
        resolved[key] = str(row["uuid"])
    mismatch: list[str] = []
    mapping = x.h.get("lab_results")
    if isinstance(mapping, Mapping):
        configured = LabResultsConfig.from_mapping(mapping)
        mismatch = [k for k, v in resolved.items() if getattr(configured, k) not in ("", v)]
    x.sample("lab_result_types", {"resolved": resolved, "datatypes": datatypes})
    ok = not bad and not mismatch
    if ok:
        _lab(x)["config"] = LabResultsConfig(
            **resolved, result_location_uuid=x.config.location_uuid
        )
    return passed(
        ok,
        f"resolved={len(resolved)}/{len(STOCK_CONCEPTS) + 1} datatypes={datatypes} "
        f"missing_or_wrong={bad} config_mismatch={sorted(mismatch)}",
    )


async def lab_result_write_readback(x: AdtCtx) -> Result:
    _cfg(x)
    test = _input(x, "numeric_test_concept_uuid")
    defn = await _definition(x, test)
    if defn.datatype != "Numeric":
        return passed(False, f"lab.numeric_test_concept_uuid datatype {defn.datatype}")
    patient, visit = await _patient_visit(x)
    order = await _place_order(x, patient, visit, test)
    at = _now()
    value = Decimal("12")
    w = _write_for(order, defn, at, value, ranges=True, abnormal=True)
    outcome = await _write(x, patient=patient, visit=visit, results=[w])
    _lab(x)["visit"] = {"patient": patient, "visit": visit}
    _lab(x)["single"] = {
        "patient": patient,
        "visit": visit,
        "order": order,
        "test": test,
        "result_at": at,
        "value": value,
        "outcome": outcome,
    }
    row = _row(await _rows(x, patient, visit), order, test)
    if row is None:
        return passed(False, "order via raw; no row for the order after the write")
    checks = {
        "resulted": row.status == "resulted",
        "value": _num(row.result) == value,
        "ranges": row.min_normal == Decimal("1") and row.max_normal == Decimal("10"),
        "abnormal": row.abnormal is True,
        "provider_set": bool(row.provider),
    }
    return passed(all(checks.values()), f"order via raw {checks}")


async def lab_result_panel_readback(x: AdtCtx) -> Result:
    _cfg(x)
    panel = _input(x, "panel_concept_uuid")
    where = _state(x, "visit")
    patient, visit = where["patient"], where["visit"]
    members = await _panel_members(x, panel)
    defs = [await _definition(x, m) for m in members]
    order = await _place_order(x, patient, visit, panel)
    at = _now()
    writes = [_write_for(order, d, at, Decimal("5"), panel) for d in defs]
    await _write(x, patient=patient, visit=visit, results=writes)
    rows = [r for r in await _rows(x, patient, visit) if r.order_uuid == order]
    with_panel = [r for r in rows if r.panel_uuid == panel and r.status == "resulted"]
    return passed(
        len(with_panel) == 2,
        f"order via raw rows={len(rows)} resulted_with_panel={len(with_panel)}",
    )


async def lab_result_newer_guard(x: AdtCtx) -> Result:
    s = _state(x, "single")
    defn = LabTestDefinition(concept_uuid=s["test"], datatype="Numeric", units=None, answers=())
    before = _row(await _rows(x, s["patient"], s["visit"]), s["order"], s["test"])
    older = _write_for(s["order"], defn, s["result_at"] - GUARD_OFFSET, Decimal("7"))
    writes_before = x.writes
    code = None
    try:
        await _write(x, patient=s["patient"], visit=s["visit"], results=[older])
    except LabResultConflictError as exc:
        code = exc.code
    posted = x.writes - writes_before
    after = _row(await _rows(x, s["patient"], s["visit"]), s["order"], s["test"])
    unchanged = (before is not None and after is not None) and (
        (before.result, before.result_at) == (after.result, after.result_at)
    )
    if posted:
        return passed(False, f"guard broken: older write posted ({posted} write call) code={code}")
    return passed(
        code == RECORD_RESULT_NEWER and unchanged,
        f"refused code={code} posted={posted} record_unchanged={unchanged}",
    )


async def lab_result_session_merge(x: AdtCtx) -> Result:
    _cfg(x)
    test = _input(x, "numeric_test_concept_uuid")
    panel = _input(x, "panel_concept_uuid")
    patient, visit = await _patient_visit(x)
    single = await _place_order(x, patient, visit, test)
    panel_order = await _place_order(x, patient, visit, panel)
    defn = await _definition(x, test)
    members = [await _definition(x, m) for m in await _panel_members(x, panel)]
    first = await _write(
        x,
        patient=patient,
        visit=visit,
        results=[_write_for(single, defn, _now(), Decimal("4"))],
        named=False,
    )
    try:
        at = _now()
        second = await _write(
            x,
            patient=patient,
            visit=visit,
            results=[_write_for(panel_order, d, at, Decimal("4"), panel) for d in members],
            named=False,
        )
    except Exception as exc:  # noqa: BLE001 - record-only: the refusal IS the finding
        return record(f"second unnamed write refused {err(exc)}")
    return record(f"same_encounter={first.encounter_uuid == second.encounter_uuid}")


async def lab_result_no_clobber_after_collect(x: AdtCtx) -> Result:
    data = x.state.get("lab_collect") or x.h.get("lab_collect")
    keys = ("patient_uuid", "visit_uuid", "order_uuid", "test_concept_uuid")
    if not isinstance(data, Mapping) or not all(isinstance(data.get(k), str) for k in keys):
        return inconclusive(
            "no collected order supplied (harness lab_collect); run after the lab "
            "system's collection step on a synthetic patient's order"
        )
    _cfg(x)
    patient, visit = str(data["patient_uuid"]), str(data["visit_uuid"])
    order, test = str(data["order_uuid"]), str(data["test_concept_uuid"])
    who = await x.raw("GET", f"patient/{patient}", params={"v": "custom:(uuid,display)"})
    display = str((who.body or {}).get("display") or "") if isinstance(who.body, dict) else ""
    if "LT-P" not in display:
        return inconclusive("lab_collect patient is not a synthetic LT- record")
    row = _row(await _rows(x, patient, visit), order, test)
    if row is None or row.status != "pending":
        return inconclusive(
            f"lab_collect order not pending (row={'none' if row is None else row.status})"
        )
    defn = await _definition(x, test)
    w = _write_for(order, defn, _now(), Decimal("6"))
    outcome = await _write(x, patient=patient, visit=visit, results=[w])
    await asyncio.sleep(FEED_WAIT_S)
    after = _row(await _rows(x, patient, visit), order, test)
    voided = []
    for obs in outcome.obs_uuids_by_order.get(order, ()):
        got = await x.raw("GET", f"obs/{obs}", params={"v": "custom:(uuid,voided)"})
        voided.append(isinstance(got.body, dict) and got.body.get("voided") is True)
    ours = (
        after is not None
        and after.status == "resulted"
        and (
            _num(after.result) == w.value if w.value_kind == "numeric" else after.result is not None
        )
    )
    return passed(
        ours and not any(voided),
        f"waited_s={FEED_WAIT_S:.0f} our_result_shown={ours} group_voided={any(voided)}",
    )


async def lab_result_void_amend(x: AdtCtx) -> Result:
    s = _state(x, "single")
    outcome: LabResultWriteOutcome = s["outcome"]
    groups = outcome.obs_uuids_by_order.get(s["order"], ())
    if not groups:
        raise PrereqMissingError("blocked: no written group for the single order")
    for obs in groups:
        await _client(x).void_lab_result(obs, VOID_REASON)
    mid = _row(await _rows(x, s["patient"], s["visit"]), s["order"], s["test"])
    pending = mid is not None and mid.status == "pending"
    defn = LabTestDefinition(concept_uuid=s["test"], datatype="Numeric", units=None, answers=())
    new = Decimal("9")
    await _write(
        x,
        patient=s["patient"],
        visit=s["visit"],
        results=[_write_for(s["order"], defn, _now(), new, ranges=True)],
    )
    end = _row(await _rows(x, s["patient"], s["visit"]), s["order"], s["test"])
    shown = end is not None and end.status == "resulted" and _num(end.result) == new
    return passed(pending and shown, f"pending_after_void={pending} new_value_shown={shown}")


# Run order: read-only resolution first; writes on this run's own patients after.
LAB_CHECKS: list[tuple[str, str, LabCheck]] = [
    (GATE, "lab_result_types", lab_result_types_resolved),
    ("lab_result_write_readback", "lab_result_write", lab_result_write_readback),
    ("lab_result_panel_readback", "lab_result_panel", lab_result_panel_readback),
    ("lab_result_newer_guard", "lab_result_newer_guard", lab_result_newer_guard),
    ("lab_result_session_merge", "lab_result_session_merge", lab_result_session_merge),
    (
        "lab_result_no_clobber_after_collect",
        "lab_result_no_clobber",
        lab_result_no_clobber_after_collect,
    ),
    ("lab_result_void_amend", "lab_result_void_amend", lab_result_void_amend),
]


def _guarded(fn: LabCheck) -> LabCheck:
    async def run(x: AdtCtx) -> Result:
        try:
            return await fn(x)
        except PrereqMissingError as exc:
            return inconclusive(str(exc))

    return run


async def run_checks(ctx: AdtCtx) -> list[CheckResult]:
    """Run every lab result check once, in order; the caller runs the cleanup."""
    out: list[CheckResult] = []
    gate_ok = True
    for name, topic, fn in LAB_CHECKS:
        if ctx.cancelled:
            res: Result = inconclusive("interrupted by a signal")
        elif name != GATE and not gate_ok:
            res = inconclusive(f"blocked: {GATE} did not pass")
        else:
            res = await _one(ctx, name, _guarded(fn))
        if name == GATE:
            gate_ok = res[0] == "PASS"
        out.append((name, topic, (res[0], scrub(res[1]))))
    return out


__all__ = [
    "FEED_WAIT_S",
    "GUARD_OFFSET",
    "LAB_CHECKS",
    "CheckResult",
    "run_checks",
]
