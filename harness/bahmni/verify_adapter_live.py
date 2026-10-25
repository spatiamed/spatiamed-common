#!/usr/bin/env python
"""Drive the real BahmniAdapter + BahmniRecordClient against a live Bahmni.

Unit tests prove the request shapes; only this proves a real Bahmni answers them.
Prints one line per check, ``CHECK <name> PASS|FAIL <detail>``; exit 0 only if all pass.

Env (required): BAHMNI_BASE_URL, BAHMNI_USER, BAHMNI_PASS, BAHMNI_CONFIG_JSON (written by
seed_data.py). Optional: BAHMNI_CA_PEM (PEM path), BAHMNI_INSECURE_TLS=1 (throwaway test
box only), HARNESS_SAMPLES_DIR (raw responses are written there as JSON),
HARNESS_CONTINUE_AFTER_GATE=1 (keep going after the marker_searchable gate fails).
Writes synthetic ``LT-ADAPTER-`` records only.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sm_common.integrations.adapters.bahmni import BahmniAdapter  # noqa: E402
from sm_common.integrations.adapters.bahmni_config import BahmniConfig  # noqa: E402
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient  # noqa: E402
from sm_common.integrations.build_adapter import (  # noqa: E402
    AdapterBuildConfig,
    build_adapter,
    build_bahmni_record_client,
)
from sm_common.integrations.canonical_types import AppointmentWrite, PatientCreate  # noqa: E402
from sm_common.integrations.exceptions import HmsAdapterError, TransientError  # noqa: E402
from sm_common.integrations.record_types import (  # noqa: E402
    RecordAddress,
    RecordDiagnosis,
    RecordDocumentInput,
    RecordDrugOrder,
    RecordEncounterInput,
    RecordObs,
    RecordPatientInput,
)

REST = "/openmrs/ws/rest/v1"
FEED = "/openmrs/ws/atomfeed/appointments/recent"
ATOM = "{http://www.w3.org/2005/Atom}"
ICD = "1A00"
GATE_MSG = "marker attribute not searchable — add it to Bahmni patient-search config"
PAGING_MAX = 60

Check = Callable[["Ctx"], Awaitable[tuple[bool, str]]]


class Ctx:
    def __init__(self, cfg: dict[str, Any], adapter: BahmniAdapter, record: BahmniRecordClient):
        self.fm: dict[str, Any] = cfg["field_mapping"]
        self.h: dict[str, Any] = cfg["harness"]
        self.tag: str = self.h["tag"]
        self.adapter = adapter
        self.record = record
        self.http = adapter._http
        self.config: BahmniConfig = adapter._config
        self.state: dict[str, Any] = {}
        self.samples = os.environ.get("HARNESS_SAMPLES_DIR")
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        # A per-run slot two hours out, minute-jittered so reruns never share a slot.
        self.slot = now + timedelta(hours=2, minutes=uuid4().int % 50)

    def sample(self, name: str, obj: Any) -> None:
        if not self.samples:
            return
        d = pathlib.Path(self.samples)
        d.mkdir(parents=True, exist_ok=True)
        text = obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str)
        (d / f"{name}.{'xml' if isinstance(obj, str) else 'json'}").write_text(text)

    async def get(self, path: str, **params: str) -> Any:
        resp = await self.http.request("GET", path, params=params or None)
        return resp.json() if resp.status_code != 404 else {"_status": 404}


# ─── checks ──────────────────────────────────────────────────────────────────


async def health(x: Ctx) -> tuple[bool, str]:
    h = await x.adapter.health_check()
    return h.healthy, f"healthy={h.healthy} latency_ms={h.latency_ms} msg={h.message}"


async def marker_searchable(x: Ctx) -> tuple[bool, str]:
    marker = uuid4()
    rec, created = await x.record.create_patient(
        RecordPatientInput(
            marker=marker,
            given=f"{x.tag} Marker",
            family=f"{x.tag}-Marker",
            gender="M",
            birth_date=date(1990, 1, 1),
            birth_date_estimated=False,
            phone=None,
            address=RecordAddress(),
        )
    )
    raw = await x.get(
        f"{REST}/bahmni/search/patient",
        q="",
        s="byIdOrNameOrVillage",
        customAttribute=f"sm:{marker}",
        patientAttributes=x.config.marker_attribute_name,
    )
    x.sample("marker_search", raw)
    hits = await x.record._find_by_marker(f"sm:{marker}")
    ok = created and len(hits) == 1 and hits[0].uuid == rec.uuid
    detail = f"created={created} hits={len(hits)} raw_total={raw.get('totalCount')}"
    return ok, detail if ok else f"{GATE_MSG} ({detail})"


async def search_phone(x: Ctx) -> tuple[bool, str]:
    hits = await x.adapter.search_patients(phone=x.h["patient_phone"])
    mine = [p for p in hits if p.resource_id == x.h["patient_uuid"]]
    ok = len(mine) == 1 and bool(mine[0].mrn)
    return (
        ok,
        (
            f"hits={len(hits)} seeded_found={bool(mine)} "
            f"mrn={'set' if mine and mine[0].mrn else 'EMPTY'}"
        ),
    )


async def hit_fields(x: Ctx) -> tuple[bool, str]:
    raw = await x.get(
        f"{REST}/bahmni/search/patient",
        q="",
        s="byIdOrNameOrVillage",
        customAttribute=x.h["patient_phone"],
        patientAttributes="phoneNumber",
    )
    x.sample("hit_fields_search", raw)
    page = raw.get("pageOfResults") or []
    hit = next((h for h in page if h.get("uuid") == x.h["patient_uuid"]), None)
    if hit is None:
        return False, f"seeded patient not in page (keys={sorted(raw)})"
    want = ("givenName", "familyName", "birthDate", "customAttribute", "birthdateEstimated")
    missing = [k for k in want if k not in hit]
    bd = hit.get("birthDate")
    epoch = isinstance(bd, int) and not isinstance(bd, bool)
    types = {k: type(hit.get(k)).__name__ for k in want}
    return not missing and epoch, f"missing={missing} types={types} envelope={sorted(raw)}"


async def _household(x: Ctx, n: int) -> None:
    for i in range(n):
        await x.record.create_patient(
            RecordPatientInput(
                marker=uuid5(NAMESPACE_URL, f"{x.tag}-paging-{i}"),
                given=f"{x.tag} Paging{i}",
                family=f"{x.tag}-Paging",
                gender="F",
                birth_date=date(1970, 1, 1) + timedelta(days=i),
                birth_date_estimated=False,
                phone=x.h["paging_phone"],
                address=RecordAddress(),
            )
        )


async def search_paging(x: Ctx) -> tuple[bool, str]:
    base = {
        "q": "",
        "s": "byIdOrNameOrVillage",
        "customAttribute": x.h["paging_phone"],
        "patientAttributes": "phoneNumber",
    }
    probe = await x.get(f"{REST}/bahmni/search/patient", q="LT-", s="byIdOrNameOrVillage")
    # Learn the server page size from a broad query; 10 if the probe finds nothing.
    size = len(probe.get("pageOfResults") or []) or 10
    if size >= PAGING_MAX:
        return False, f"page size {size} >= {PAGING_MAX}; not creating that many patients"
    await _household(x, size + 2)
    p0 = await x.get(f"{REST}/bahmni/search/patient", **base, startIndex="0")
    page0 = p0.get("pageOfResults") or []
    p1 = await x.get(f"{REST}/bahmni/search/patient", **base, startIndex=str(len(page0)))
    page1 = p1.get("pageOfResults") or []
    x.sample("search_paging_p0", p0)
    x.sample("search_paging_p1", p1)
    u0 = {h["uuid"] for h in page0}
    u1 = {h["uuid"] for h in page1}
    total = p0.get("totalCount")
    via_adapter = await x.adapter.search_patients(phone=x.h["paging_phone"])
    ok = (
        isinstance(total, int)
        and total > len(page0)
        and bool(u1)
        and not (u0 & u1)
        and len(via_adapter) >= size + 2
    )
    return ok, (
        f"page_size={size} probe_total={probe.get('totalCount')} household={size + 2} "
        f"p0={len(page0)} p1={len(page1)} totalCount={total} overlap={len(u0 & u1)} "
        f"adapter_hits={len(via_adapter)}"
    )


async def profile_get(x: Ctx) -> tuple[bool, str]:
    uuid = x.h["patient_uuid"]
    raw = await x.get(f"{REST}/patientprofile/{uuid}", v="full")
    x.sample("profile_get", raw)
    via_adapter = await x.adapter.get_patient(uuid)
    via_record = await x.record.get_patient(uuid)
    ok = (
        via_adapter is not None
        and via_adapter.mrn == x.h["patient_identifier"]
        and via_record is not None
        and via_record.phone == x.h["patient_phone"]
        and via_record.birth_date is not None
    )
    return ok, (
        f"envelope={sorted(raw)} "
        f"adapter_mrn_match={bool(via_adapter and via_adapter.mrn == x.h['patient_identifier'])} "
        f"record_phone_match={bool(via_record and via_record.phone == x.h['patient_phone'])} "
        f"dob={via_record.birth_date if via_record else None}"
    )


async def create_patient_marker(x: Ctx) -> tuple[bool, str]:
    pc = PatientCreate(
        patient_marker=uuid4(),
        family=f"{x.tag}-Create",
        given=[f"{x.tag} Create"],
        birth_date=date(1985, 5, 5),
        gender="F",
        phone=None,
    )
    r1 = await x.adapter.create_patient(pc)
    r2 = await x.adapter.create_patient(pc)
    ok = r1.created and not r2.created and r1.resource_id == r2.resource_id and bool(r1.mrn)
    return (
        ok,
        (
            f"first.created={r1.created} second.created={r2.created} "
            f"same_uuid={r1.resource_id == r2.resource_id} mrn={'set' if r1.mrn else 'EMPTY'}"
        ),
    )


async def create_identifier(x: Ctx) -> tuple[bool, str]:
    configured = x.config.identifier_prefix
    outcomes: dict[str, str] = {}
    accepted: dict[str, bool] = {}
    for label, prefix in (("with_prefix", configured), ("without_prefix", "")):
        if label == "with_prefix" and not prefix:
            outcomes[label] = "skipped (no prefix configured)"
            continue
        client = BahmniRecordClient(x.http, replace(x.config, identifier_prefix=prefix))
        try:
            rec, _ = await client.create_patient(
                RecordPatientInput(
                    marker=uuid4(),
                    given=f"{x.tag} Ident",
                    family=f"{x.tag}-Ident",
                    gender="M",
                    birth_date=date(1975, 3, 3),
                    birth_date_estimated=False,
                    phone=None,
                    address=RecordAddress(),
                )
            )
            accepted[label] = True
            outcomes[label] = f"accepted identifier={rec.identifier!r}"
        except HmsAdapterError as exc:
            accepted[label] = False
            outcomes[label] = f"rejected {type(exc).__name__}: {str(exc)[:160]}"
    key = "with_prefix" if configured else "without_prefix"
    return accepted.get(key, False), f"configured_prefix={configured!r} {outcomes}"


def _write(
    x: Ctx, booking_id: Any, offset_min: int = 0, service: str | None = None
) -> AppointmentWrite:
    start = x.slot + timedelta(minutes=offset_min)
    return AppointmentWrite(
        booking_id=booking_id,
        patient_ref=x.h["patient_uuid"],
        practitioner_ref=x.h["provider_uuid"],
        start=start,
        end=start + timedelta(minutes=15),
        reason="harness booking",
        service_ref=service,
    )


async def write_back(x: Ctx) -> tuple[bool, str]:
    x.state["b1"] = uuid4()
    r = await x.adapter.write_back_idempotent(_write(x, x.state["b1"]))
    x.state["a1"] = r.hms_booking_id
    ok = r.status == "SUCCESS" and r.created and r.hms_start == x.slot
    return ok, f"status={r.status} created={r.created} start_roundtrip={r.hms_start == x.slot}"


async def write_back_retry_same_uuid(x: Ctx) -> tuple[bool, str]:
    r = await x.adapter.write_back_idempotent(_write(x, x.state["b1"]))
    ok = r.status == "SUCCESS" and not r.created and r.hms_booking_id == x.state["a1"]
    return ok, f"created={r.created} same_uuid={r.hms_booking_id == x.state['a1']}"


async def feed_sees_booking(x: Ctx) -> tuple[bool, str]:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    polls = 0
    while True:
        polls += 1
        appts, cursor = await x.adapter.list_appointments_modified_since("", date.today())
        if any(a.appointment_id == x.state["a1"] for a in appts):
            x.state["feed_cursor"] = cursor
            return True, f"seen after {loop.time() - t0:.1f}s polls={polls} entries={len(appts)}"
        if loop.time() - t0 > 10:
            return False, f"not on the recent page after {polls} polls / 10s ({len(appts)} entries)"
        await asyncio.sleep(1)


async def feed_page_order(x: Ctx) -> tuple[bool, str]:
    resp = await x.http.request("GET", FEED, expect=(200,))
    x.sample("feed_recent", resp.text)
    root = ET.fromstring(resp.text)
    updated = [(e.findtext(f"{ATOM}updated") or "").strip() for e in root.findall(f"{ATOM}entry")]
    if len(updated) < 2:
        return False, f"only {len(updated)} entries on the recent page"
    parsed = [datetime.fromisoformat(u.replace("Z", "+00:00")) for u in updated]
    if parsed == sorted(parsed):
        order = "oldest-first"
    elif parsed == sorted(parsed, reverse=True):
        order = "newest-first"
    else:
        order = "unordered"
    # The adapter orders a page by comparing <updated> as strings: that must equal time order.
    lexical_ok = [u for _, u in sorted(zip(parsed, updated, strict=True))] == sorted(updated)
    links = {link.get("rel"): link.get("href") for link in root.findall(f"{ATOM}link")}
    ok = order != "unordered" and lexical_ok
    return ok, (
        f"order={order} entries={len(updated)} format_example={updated[-1]!r} "
        f"lexical_equals_chronological={lexical_ok} link_rels={sorted(k for k in links if k)}"
    )


async def search_window_semantics(x: Ctx) -> tuple[bool, str]:
    a1 = x.state["a1"]
    inside, _ = await x.adapter._search_window(
        x.slot + timedelta(minutes=5), x.slot + timedelta(minutes=10)
    )
    overlap = any(r.get("uuid") == a1 for r in inside)
    around, cut = await x.adapter._search_window(
        x.slot - timedelta(minutes=1), x.slot + timedelta(minutes=16)
    )
    row = next((r for r in around if r.get("uuid") == a1), None)
    full = await x.get(f"{REST}/appointments/{a1}")
    x.sample("search_row", row)
    x.sample("appointment_get", full)
    if row is None:
        return False, f"booking not found in a window around its slot (truncated={cut})"
    shape: dict[str, str] = {}
    for k in ("dateChanged", "dateCreated", "providers", "startDateTime", "comments", "status"):
        shape[k] = f"{type(row.get(k)).__name__}/{type(full.get(k)).__name__}"
    prov_row = row.get("providers") or []
    prov_full = full.get("providers") or []
    prov_same = bool(prov_row) and (prov_row[0] or {}).get("uuid") == (prov_full[0] or {}).get(
        "uuid"
    )
    has_change = row.get("dateChanged") is not None or row.get("dateCreated") is not None
    ok = prov_same and has_change
    return ok, (
        f"filter={'overlap' if overlap else 'slot-start'} whole_second_iso=accepted "
        f"providers_same={prov_same} shapes(search/get)={shape}"
    )


async def recent_bookings(x: Ctx) -> tuple[bool, str]:
    rows = await x.adapter.fetch_recent_bookings(uuid4(), lookback_minutes=60)
    mine = [b for b in rows if b.appointment_id == x.state["a1"]]
    if not mine:
        return False, f"booking absent from {len(rows)} rows"
    b = mine[0]
    age = datetime.now(UTC) - b.updated_at
    ok = (
        b.doctor_external_id == x.h["provider_uuid"]
        and b.slot_start == x.slot
        and -timedelta(minutes=5) <= age <= timedelta(hours=1)
    )
    return (
        ok,
        (
            f"rows={len(rows)} doctor_ok={b.doctor_external_id == x.h['provider_uuid']} "
            f"slot_ok={b.slot_start == x.slot} updated_age_s={age.total_seconds():.0f}"
        ),
    )


async def cancel(x: Ctx) -> tuple[bool, str]:
    r = await x.adapter.cancel(x.state["a1"], "harness cancel")
    status = (await x.get(f"{REST}/appointments/{x.state['a1']}")).get("status")
    return (
        r.status == "SUCCESS" and status == "Cancelled",
        f"result={r.status} bahmni_status={status}",
    )


async def cancel_again_success(x: Ctx) -> tuple[bool, str]:
    r = await x.adapter.cancel(x.state["a1"], "harness cancel again")
    return r.status == "SUCCESS", f"result={r.status} {r.error_detail or ''}".strip()


async def write_back_after_cancel_new_uuid(x: Ctx) -> tuple[bool, str]:
    r = await x.adapter.write_back_idempotent(_write(x, x.state["b1"]))
    ok = r.status == "SUCCESS" and r.created and r.hms_booking_id != x.state["a1"]
    return ok, f"created={r.created} new_uuid={r.hms_booking_id != x.state['a1']}"


async def write_back_service_ref(x: Ctx) -> tuple[bool, str]:
    second = x.h["service_uuids"][1]
    r = await x.adapter.write_back_idempotent(_write(x, uuid4(), offset_min=30, service=second))
    got = (await x.get(f"{REST}/appointments/{r.hms_booking_id}")).get("service") or {}
    return got.get(
        "uuid"
    ) == second, f"created={r.created} service_matches={got.get('uuid') == second}"


async def start_visit(x: Ctx) -> tuple[bool, str]:
    v1, c1 = await x.record.start_visit(x.h["patient_uuid"])
    v2, c2 = await x.record.start_visit(x.h["patient_uuid"])
    x.state["visit"] = v1.uuid
    ok = v1.uuid == v2.uuid and not c2 and v1.stop is None
    return (
        ok,
        (
            f"first.created={c1} second.created={c2} "
            f"same_visit={v1.uuid == v2.uuid} type={v1.visit_type}"
        ),
    )


DRUGS = (
    RecordDrugOrder("LT-ADAPTER Paracetamol 500", "1 tab", "TDS", 5, "after food"),
    RecordDrugOrder("LT-ADAPTER Cetirizine 10", "1 tab", "OD", 3, "at night"),
)


def _encounter(x: Ctx, note_id: Any) -> RecordEncounterInput:
    obs = [RecordObs("vitals_pulse", 72.0)]
    if "chief_complaint" in x.config.concept_map:
        obs.append(RecordObs("chief_complaint", "LT-ADAPTER synthetic complaint"))
    return RecordEncounterInput(
        note_id=note_id,
        patient_uuid=x.h["patient_uuid"],
        visit_uuid=x.state["visit"],
        provider_uuid=x.h["provider_uuid"],
        encounter_at=datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1),
        obs=obs,
        diagnoses=[RecordDiagnosis(code=ICD, title="LT-ADAPTER synthetic fever", primary=True)],
        drug_orders=list(DRUGS),
    )


async def write_encounter(x: Ctx) -> tuple[bool, str]:
    x.state["note"] = uuid4()
    r1 = await x.record.write_encounter(_encounter(x, x.state["note"]))
    x.state["enc"] = r1.encounter_uuid
    r2 = await x.record.write_encounter(_encounter(x, x.state["note"]))
    ok = r1.created and not r2.created and r1.encounter_uuid == r2.encounter_uuid
    return (
        ok,
        (
            f"first.created={r1.created} second.created={r2.created} "
            f"same_uuid={r1.encounter_uuid == r2.encounter_uuid} (no action field sent)"
        ),
    )


async def diagnosis_comment_persisted(x: Ctx) -> tuple[bool, str]:
    raw = await x.get(
        f"{REST}/encounter/{x.state['enc']}",
        v="custom:(uuid,obs:(concept:(display),value,comment,groupMembers:(concept:(display),value,comment)),orders:(drugNonCoded,action))",
    )
    x.sample("encounter_readback", raw)
    detail = await x.record.get_visit(x.state["visit"])
    enc = next((e for e in detail.encounters if e.uuid == x.state["enc"]), None) if detail else None
    if enc is None:
        return False, "encounter not in get_visit"
    x.state["drugs_read"] = enc.drugs
    comments = [c for _, c in enc.diagnoses]
    return f"ICD-11 {ICD}" in comments, f"diagnoses={enc.diagnoses} drugs={len(enc.drugs)}"


async def encounter_write_live(x: Ctx) -> tuple[bool, str]:
    drugs = x.state.get("drugs_read") or []
    both = all(d.name in drugs for d in DRUGS)
    try:
        r = await x.record.write_encounter(_encounter(x, uuid4()))
        detail = await x.record.get_visit(x.state["visit"])
        enc = next(
            (e for e in (detail.encounters if detail else []) if e.uuid == r.encounter_uuid), None
        )
        repeat = f"accepted (new encounter, drugs on it={len(enc.drugs) if enc else '?'})"
    except HmsAdapterError as exc:
        repeat = f"rejected {type(exc).__name__}: {str(exc)[:200]}"
    needed = "no (accepted without it)" if both else "unknown"
    return both, (
        f"both_orders_read_back={both} action_NEW_needed={needed} repeat_same_drug_concept={repeat}"
    )


def _pdf() -> bytes:
    """A valid one-page PDF with correct xref offsets."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 12 Tf 20 100 Td (LT-ADAPTER synthetic document) Tj ET"
    objs[3] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + (body or b"") + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


async def upload_document(x: Ctx) -> tuple[bool, str]:
    r = await x.record.upload_document(
        RecordDocumentInput(
            patient_uuid=x.h["patient_uuid"],
            content=_pdf(),
            mime="application/pdf",
            doc_type=x.h["doc_type"],
            provider_uuid=x.h["provider_uuid"],
            taken_at=datetime.now(UTC).replace(microsecond=0),
        )
    )
    return bool(r.visit_uuid and r.encounter_uuid and r.url), (
        f"visit={'set' if r.visit_uuid else 'EMPTY'} "
        f"encounter={'set' if r.encounter_uuid else 'EMPTY'} "
        f"url_ext={r.url.rsplit('.', 1)[-1]} concept={x.h['doc_concept_name']!r}"
    )


async def allergies(x: Ctx) -> tuple[bool, str]:
    names = await x.record.allergies(x.h["patient_uuid"])
    return True, f"allergies={'UNKNOWN (route 404)' if names is None else len(names)}"


async def provider_paging(x: Ctx) -> tuple[bool, str]:
    p0 = await x.get(f"{REST}/provider", v="full", limit="1", startIndex="0")
    p1 = await x.get(f"{REST}/provider", v="full", limit="1", startIndex="1")
    x.sample("provider_p0", p0)
    rels = [link.get("rel") for link in p0.get("links") or [] if isinstance(link, dict)]
    u0 = [r.get("uuid") for r in p0.get("results") or []]
    u1 = [r.get("uuid") for r in p1.get("results") or []]
    ok = "next" in rels and bool(u1) and u0 != u1
    return ok, f"rels={rels} p0={len(u0)} p1={len(u1)} distinct={u0 != u1}"


async def roster_no_system_providers(x: Ctx) -> tuple[bool, str]:
    roster = await x.adapter.fetch_doctor_roster(date.today())
    idents = {i for d in roster for _, i in d.identifiers}
    leaked = idents & set(x.config.system_provider_identifiers)
    seeded = any(d.external_doctor_id == x.h["provider_uuid"] for d in roster)
    mapped = next((d for d in roster if d.external_doctor_id == x.h["provider_uuid"]), None)
    label = mapped.speciality_label if mapped else ""
    return (
        not leaked and seeded,
        (
            f"doctors={len(roster)} system_leaked={len(leaked)} "
            f"seeded_present={seeded} speciality_label={label!r}"
        ),
    )


CHECKS: list[tuple[str, Check]] = [
    ("health", health),
    ("marker_searchable", marker_searchable),
    ("search_phone", search_phone),
    ("hit_fields", hit_fields),
    ("search_paging", search_paging),
    ("profile_get", profile_get),
    ("create_patient_marker", create_patient_marker),
    ("create_identifier", create_identifier),
    ("write_back", write_back),
    ("write_back_retry_same_uuid", write_back_retry_same_uuid),
    ("feed_sees_booking", feed_sees_booking),
    ("feed_page_order", feed_page_order),
    ("search_window_semantics", search_window_semantics),
    ("recent_bookings", recent_bookings),
    ("cancel", cancel),
    ("cancel_again_success", cancel_again_success),
    ("write_back_after_cancel_new_uuid", write_back_after_cancel_new_uuid),
    ("write_back_service_ref", write_back_service_ref),
    ("start_visit", start_visit),
    ("write_encounter", write_encounter),
    ("diagnosis_comment_persisted", diagnosis_comment_persisted),
    ("encounter_write_live", encounter_write_live),
    ("upload_document", upload_document),
    ("allergies", allergies),
    ("provider_paging", provider_paging),
    ("roster_no_system_providers", roster_no_system_providers),
]


async def run_check(x: Ctx, fn: Check) -> tuple[bool, str]:
    for attempt in (1, 2):
        try:
            return await fn(x)
        except TransientError as exc:
            # A dropped port-forward reads as a transport error: one retry after a pause.
            if attempt == 2:
                return False, f"TransientError: {exc}"
            await asyncio.sleep(5)
        except KeyError as exc:
            return False, f"prerequisite missing: {exc}"
        except Exception as exc:  # noqa: BLE001 - every failure is a reported FAIL
            return False, f"{type(exc).__name__}: {str(exc)[:300]}"
    return False, "unreachable"


def build(cfg: dict[str, Any]) -> tuple[BahmniAdapter, BahmniRecordClient]:
    creds: dict[str, Any] = {
        "username": os.environ["BAHMNI_USER"],
        "password": os.environ["BAHMNI_PASS"],
        "auth_scheme": "basic",
    }
    if os.environ.get("BAHMNI_CA_PEM"):
        creds["ca_bundle_pem"] = pathlib.Path(os.environ["BAHMNI_CA_PEM"]).read_text()
    elif os.environ.get("BAHMNI_INSECURE_TLS") == "1":
        creds["verify_tls"] = False
        creds["allow_insecure_tls"] = True
    bc = AdapterBuildConfig(
        vendor="bahmni",
        base_url=os.environ["BAHMNI_BASE_URL"],
        credentials=creds,
        field_mapping=cfg["field_mapping"],
        hash_salt="harness-salt",
    )
    adapter = build_adapter(bc)
    assert isinstance(adapter, BahmniAdapter)
    # The record client shares the adapter's session (one login for the run).
    record = BahmniRecordClient(adapter._http, adapter._config)
    build_bahmni_record_client(bc)  # proves the public factory builds too
    return adapter, record


async def main() -> int:
    need = ("BAHMNI_BASE_URL", "BAHMNI_USER", "BAHMNI_PASS", "BAHMNI_CONFIG_JSON")
    missing = [n for n in need if not os.environ.get(n)]
    if missing:
        print(f"set {', '.join(missing)}", file=sys.stderr)
        return 2
    cfg = json.loads(pathlib.Path(os.environ["BAHMNI_CONFIG_JSON"]).read_text())
    adapter, record = build(cfg)
    x = Ctx(cfg, adapter, record)
    failed = 0
    try:
        for name, fn in CHECKS:
            ok, detail = await run_check(x, fn)
            print(f"CHECK {name} {'PASS' if ok else 'FAIL'} {detail}", flush=True)
            failed += not ok
            if name == "marker_searchable" and not ok:
                print(f"SETUP GATE FAILED: {GATE_MSG}", flush=True)
                if os.environ.get("HARNESS_CONTINUE_AFTER_GATE") != "1":
                    return 1
    finally:
        await adapter._http.aclose()
    print(f"SUMMARY {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
