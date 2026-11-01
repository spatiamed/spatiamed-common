"""Radiology live checks: the ``BahmniRadiologyClient`` against a live Bahmni.

Drives the real radiology client (procedures, orders, report, image studies, document
filing) on one synthetic patient named ``LT-P4-<RUN>`` and reports one row per check
as ``(name, topic, (status, detail))``; the caller prints them as
``CHECK <name> <status> <topic> <detail>``. Statuses and detail rules are those of
``adt_checks.py``: detail is safe to paste into a public PR (no uuids, no server text).

Configuration comes from the context only: the harness config's ``harness.radiology``
mapping holds the ``RadiologyConfig`` keys; provider, location, OPD visit type and
consultation encounter type come from the same ``AdtCtx`` the ADT group uses.

Shared-box safety:
- Only this run's ``LT-P4-`` patient is written to. The imaging system is never called
  directly: everything goes through ``/openmrs``. Nothing here seeds or reads the
  imaging bridge's own tables; ``radiology_order_reaches_imaging`` records the order
  number for an operator to look up there. Cleanup discontinues that order at the end of
  the same run, so the operator sees the NEW message followed by a discontinue there.
- Cleanup runs at the end of ``run_checks`` whatever happened: the filed document
  encounter is voided, every report obs this run wrote is voided, every active
  radiology order on the run's visit is discontinued (found on the visit, so an order
  whose placement answer was lost is stopped too), and last the patient is voided.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from adt_checks import (
    RUN,
    AdtCtx,
    Result,
    _cleanup,
    _one,
    passed,
    record,
    scrub,
)

from sm_common.integrations.adapters.bahmni_radiology import (
    MAX_ACCESSION_BYTES,
    BahmniRadiologyClient,
    OrderNumberTooLong,
    RadiologyConfig,
)
from sm_common.integrations.exceptions import ConfigError, NotFound
from sm_common.integrations.radiology_types import RadiologyOrderWrite, RadiologyProcedure
from sm_common.integrations.record_types import RecordAddress, RecordPatientInput

TAG = "LT-P4-"
NAME = f"{TAG}{RUN}"
# The imaging bridge polls the encounter feed every 15 s; give it two rounds.
IMAGING_WAIT_S = 30
RADIOLOGY_ORDER_TYPE_NAME = "Radiology Order"
# The synthetic procedure the box provisioning maps; any mapped one is used otherwise.
PREFERRED_PROCEDURE = "LT-P4-XRAY-CHEST"
PACS_CONFIG_GP = "pacsquery.pacsConfig"
CONFIG_KEYS = (
    "radiology_order_type_uuid",
    "orderable_set_uuid",
    "report_concept_uuid",
    "report_encounter_type_uuid",
    "document_encounter_type_uuid",
)
# Nested groups last in every custom representation (OpenMRS's parser breaks after "))").
OBS_BY_ORDER_REP = "custom:(uuid,voided,order:(uuid),encounter:(uuid,encounterType:(uuid,name)))"
DOC_ENCOUNTER_REP = "custom:(uuid,visit:(uuid),encounterType:(uuid,name))"
# A one-page blank PDF, synthetic.
SYNTHETIC_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)

CheckResult = tuple[str, str, Result]
RadCheck = Callable[[AdtCtx], Awaitable[Result]]


def _ref(v: Any) -> str | None:
    return v.get("uuid") if isinstance(v, dict) and isinstance(v.get("uuid"), str) else None


def _config(x: AdtCtx) -> RadiologyConfig:
    return RadiologyConfig.from_mapping(x.h.get("radiology"))


def _client(x: AdtCtx, *, admin: bool = False) -> BahmniRadiologyClient:
    http = x.admin_http if admin and x.admin_http is not None else x.http
    return BahmniRadiologyClient(http, _config(x))


async def _patient(x: AdtCtx) -> tuple[str, str, str]:
    """(patient uuid, identifier, visit uuid) of this run's one synthetic patient."""
    if "rad_patient" in x.state:
        got: tuple[str, str, str] = x.state["rad_patient"]
        return got
    label = f"{NAME} Imaging"
    rec, _ = await x.record.create_patient(
        RecordPatientInput(
            marker=uuid4(),
            given=label,
            family=NAME,
            gender="F",
            birth_date=date(1985, 1, 1),
            birth_date_estimated=False,
            phone=None,
            address=RecordAddress(),
        )
    )
    x.later(0, "patient", label, lambda: x._drop_patient(rec.uuid))
    visit, _ = await x.record.start_visit(rec.uuid)
    # Registered after the patient, priority -1: runs before the patient is voided.
    x.later(-1, "radiology orders", label, lambda: _stop_orders(x, rec.uuid, visit.uuid))
    x.state["rad_patient"] = (rec.uuid, rec.identifier, visit.uuid)
    return x.state["rad_patient"]


# ─── cleanup ─────────────────────────────────────────────────────────────────


async def _stop_orders(x: AdtCtx, patient: str, visit: str) -> str | None:
    """Discontinue every active radiology order on the run's visit, then confirm."""
    cfg = _config(x)
    if any(not getattr(cfg, k) for k in CONFIG_KEYS):
        # No order can be placed without the config, so none can be left behind.
        return "SKIPPED radiology config missing"
    client = _client(x, admin=True)
    failed = 0
    for row in await client.find_radiology_orders(visit):
        if row.action != "NEW" or row.date_stopped is not None:
            continue
        try:
            await client.discontinue_radiology_order(
                order_uuid=row.order_uuid,
                patient_uuid=patient,
                visit_uuid=visit,
                encounter_type_uuid=x.config.consultation_encounter_type_uuid,
                provider_uuid=x.provider,
                location_uuid=x.config.location_uuid,
                reason=f"{NAME} harness cleanup",
            )
        except Exception:  # noqa: BLE001 - counted, confirmed by the re-read below
            failed += 1
    left = [
        r
        for r in await client.find_radiology_orders(visit)
        if r.action == "NEW" and r.date_stopped is None
    ]
    if left:
        return f"{len(left)} order(s) still active ({failed} discontinue error(s))"
    return None


async def _void_report(x: AdtCtx, obs_uuid: str) -> str | None:
    if obs_uuid in x.state.setdefault("rad_voided", set()):
        return None
    try:
        await _client(x, admin=True).void_radiology_report(obs_uuid, f"{NAME} harness cleanup")
    except NotFound:
        return None
    x.state["rad_voided"].add(obs_uuid)
    return None


# ─── checks ──────────────────────────────────────────────────────────────────


async def radiology_order_type_resolved(x: AdtCtx) -> Result:
    cfg = _config(x)
    missing = [k for k in CONFIG_KEYS if not getattr(cfg, k)]
    if missing:
        return passed(False, f"radiology config missing {','.join(missing)}")
    ot = await x.raw(
        "GET", f"ordertype/{cfg.radiology_order_type_uuid}", params={"v": "custom:(uuid,name)"}
    )
    name = ot.body.get("name") if ot.ok and isinstance(ot.body, dict) else None
    procedures = await _client(x).list_procedures(limit=200)
    mapped = [p for p in procedures if p.pacs_mapped]
    chosen: RadiologyProcedure | None = next(
        (p for p in mapped if p.name == PREFERRED_PROCEDURE), mapped[0] if mapped else None
    )
    if chosen is not None:
        x.state["rad_procedure"] = chosen
    ok = name == RADIOLOGY_ORDER_TYPE_NAME and chosen is not None
    return passed(
        ok,
        f"order_type={name!r} ({ot.status}) procedures={len(procedures)} mapped={len(mapped)} "
        f"unmapped={len(procedures) - len(mapped)} "
        f"chosen={chosen.name if chosen else None!r}",
    )


async def radiology_order_placed(x: AdtCtx) -> Result:
    proc: RadiologyProcedure = x.state["rad_procedure"]
    patient, _, visit = await _patient(x)
    client = _client(x)
    try:
        placed = await client.place_radiology_orders(
            patient_uuid=patient,
            visit_uuid=visit,
            encounter_type_uuid=x.config.consultation_encounter_type_uuid,
            provider_uuid=x.provider,
            location_uuid=x.config.location_uuid,
            care_setting="OUTPATIENT",
            orders=[
                RadiologyOrderWrite(
                    concept_uuid=proc.concept_uuid, comment_to_fulfiller=f"{NAME} harness"
                )
            ],
        )
    except OrderNumberTooLong as exc:
        lens = ",".join(str(len(p.order_number.encode())) for p in exc.too_long)
        return passed(
            False,
            f"order_number_too_long bytes={lens} max={MAX_ACCESSION_BYTES} "
            f"discontinued={len(exc.discontinued)}/{len(exc.too_long)}",
        )
    order = placed[0]
    x.state["rad_order"] = order
    x.state["rad_placed_at"] = time.monotonic()
    rows = await client.find_radiology_orders(visit)
    found = any(
        r.order_uuid == order.order_uuid and r.action == "NEW" and r.date_stopped is None
        for r in rows
    )
    size = len(order.order_number.encode())
    # Second guard only: the client already raises OrderNumberTooLong above this size.
    return passed(
        size <= MAX_ACCESSION_BYTES and found,
        f"order_number_bytes={size} found_by_visit={found} visit_radiology_rows={len(rows)}",
    )


async def radiology_order_reaches_imaging(x: AdtCtx) -> Result:
    order = x.state["rad_order"]
    wait = IMAGING_WAIT_S - (time.monotonic() - x.state["rad_placed_at"])
    if wait > 0:
        await asyncio.sleep(wait)
    got = await x.raw(
        "GET", f"order/{order.order_uuid}", params={"v": "custom:(uuid,orderNumber,dateStopped)"}
    )
    body = got.body if isinstance(got.body, dict) else {}
    if not got.ok or body.get("dateStopped") is not None:
        return passed(False, f"order not active after the wait ({got.shape()})")
    # Delivery to the imaging system is not visible through the record: the operator
    # looks this order number up in the imaging bridge's test_order table.
    return record(
        f"order_number={order.order_number} waited_s={IMAGING_WAIT_S}; "
        "delivery not visible via the record: look it up in the imaging bridge test_order table "
        "(cleanup discontinues it at the end of this run, so a discontinue message follows "
        "the NEW one there)"
    )


async def pacs_studies_endpoint(x: AdtCtx) -> Result:
    _, identifier, _ = await _patient(x)
    try:
        studies = await _client(x).list_pacs_studies(identifier)
    except ConfigError as exc:
        if exc.code == "pacs_not_configured":
            return passed(
                False, f"pacs_not_configured: global property {PACS_CONFIG_GP} unset (HTTP 501)"
            )
        raise
    order = x.state.get("rad_order")
    matched = sum(1 for s in studies if order is not None and s.accession == order.order_number)
    return record(f"studies={len(studies)} matching_order_accession={matched}")


async def radiology_report_readback(x: AdtCtx) -> Result:
    order = x.state["rad_order"]
    patient, _, visit = await _patient(x)
    cfg = _config(x)
    out = await _client(x).write_radiology_report(
        order_uuid=order.order_uuid,
        patient_uuid=patient,
        visit_uuid=visit,
        provider_uuid=x.provider,
        location_uuid=x.config.location_uuid,
        report_at=datetime.now(UTC),
        findings=f"{NAME} synthetic findings.",
        impression="Synthetic impression.",
    )
    x.state["rad_report"] = out
    x.later(-1, "radiology report", NAME, lambda: _void_report(x, out.obs_uuid))
    got = await x.raw(
        "GET",
        "obs",
        params={"patient": patient, "concept": cfg.report_concept_uuid, "v": OBS_BY_ORDER_REP},
    )
    rows = [
        r
        for r in got.results()
        if _ref(r.get("order")) == order.order_uuid and r.get("voided") is not True
    ]
    hit = next((r for r in rows if r.get("uuid") == out.obs_uuid), None)
    enc = hit.get("encounter") if hit else None
    etype = enc.get("encounterType") if isinstance(enc, dict) else None
    type_name = etype.get("name") if isinstance(etype, dict) else None
    configured = _ref(etype) == cfg.report_encounter_type_uuid
    return passed(
        got.ok and hit is not None and len(rows) == 1,
        f"obs_by_order={len(rows)} read_back={hit is not None} "
        f"record encounter_type={type_name!r} is_configured_type={configured}",
    )


async def radiology_report_void(x: AdtCtx) -> Result:
    out = x.state["rad_report"]
    await _client(x).void_radiology_report(out.obs_uuid, f"{NAME} harness amendment")
    got = await x.raw("GET", f"obs/{out.obs_uuid}", params={"v": "custom:(uuid,voided)"})
    voided = got.ok and isinstance(got.body, dict) and got.body.get("voided") is True
    if voided:
        x.state.setdefault("rad_voided", set()).add(out.obs_uuid)
    return passed(voided, f"voided={voided} ({got.shape()})")


async def radiology_document_filed(x: AdtCtx) -> Result:
    proc: RadiologyProcedure = x.state["rad_procedure"]
    patient, _, visit = await _patient(x)
    cfg = _config(x)
    ref = await _client(x).upload_radiology_document(
        patient_uuid=patient,
        visit_uuid=visit,
        visit_type_uuid=x.config.opd_visit_type_uuid,
        provider_uuid=x.provider,
        location_uuid=x.config.location_uuid,
        test_concept_uuid=proc.concept_uuid,
        content=SYNTHETIC_PDF,
        mime_type="application/pdf",
        file_name=f"{NAME}-film.pdf",
        taken_at=datetime.now(UTC),
        comment=f"{NAME} outside film",
    )
    x.later(-1, "document encounter", NAME, lambda: x._delete(f"encounter/{ref.encounter_uuid}"))
    got = await x.raw("GET", f"encounter/{ref.encounter_uuid}", params={"v": DOC_ENCOUNTER_REP})
    body = got.body if got.ok and isinstance(got.body, dict) else {}
    etype = body.get("encounterType")
    type_name = etype.get("name") if isinstance(etype, dict) else None
    type_ok = _ref(etype) == cfg.document_encounter_type_uuid
    same_visit = _ref(body.get("visit")) == visit
    url_ok = bool(ref.file_url)
    return passed(
        type_ok and same_visit and url_ok,
        f"encounter_type={type_name!r} is_configured_type={type_ok} same_visit={same_visit} "
        f"url_returned={url_ok}",
    )


# (name, topic, check): run in this order; later checks build on earlier state.
# Wire this group through run_checks, not by looping over CHECKS: cleanup (discontinuing
# orders, voiding reports, documents and the patient) runs only in run_checks.
CHECKS: list[tuple[str, str, RadCheck]] = [
    ("radiology_order_type_resolved", "imaging_order_setup", radiology_order_type_resolved),
    ("radiology_order_placed", "imaging_order_place", radiology_order_placed),
    ("radiology_order_reaches_imaging", "imaging_delivery", radiology_order_reaches_imaging),
    ("pacs_studies_endpoint", "image_study_query", pacs_studies_endpoint),
    ("radiology_report_readback", "imaging_report", radiology_report_readback),
    ("radiology_report_void", "imaging_report", radiology_report_void),
    ("radiology_document_filed", "imaging_document", radiology_document_filed),
]


async def run_checks(ctx: AdtCtx) -> list[CheckResult]:
    """Run every check in order, then clean up what this group created.

    Cleanups the context already held are kept for their own runner; this group's are
    run here and removed, so nothing is undone twice.
    """
    prior = ctx.cleanups
    ctx.cleanups = []
    rows: list[CheckResult] = []
    try:
        for name, topic, fn in CHECKS:
            status, detail = await _one(ctx, name, fn)
            rows.append((name, topic, (status, scrub(detail))))
    finally:
        try:
            done, total = await _cleanup(ctx)
            ctx.state["rad_cleanup"] = (done, total)
            if done < total:
                print(
                    f"CLEANUP INCOMPLETE {total - done} left; find them by the name {NAME}",
                    flush=True,
                )
        finally:
            ctx.cleanups = prior
    return rows
