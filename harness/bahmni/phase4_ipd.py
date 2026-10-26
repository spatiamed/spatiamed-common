"""Inpatient care live checks: the ``BahmniIpdCareClient`` against a live Bahmni.

Drives the real inpatient care client (ward board, ward patients, nursing chart,
medication chart and administrations) on this run's own synthetic ward, beds and
patients, all named ``LT-P4-<RUN>``, and reports one row per check as
``(name, topic, (status, detail))``; the caller prints them as
``CHECK <name> <status> <topic> <detail>``. Statuses and detail rules are those of
``adt_checks.py``: detail is safe to paste into a public PR (no uuids, no server text).
A 403 is INCONCLUSIVE with the privilege Bahmni names; the harness never grants one.

Configuration comes from the context only: the harness config's ``harness.nursing``
mapping holds the ``NursingConfig`` keys; ``harness.ipd`` may name the dose unit, route
and frequency concepts the drug orders use (stock Bahmni names otherwise). Location,
Visit Location, consultation encounter type, the non-coded drug concept and quantity
units come from the same ``AdtCtx`` the ADT group uses.

Setup: one 1x3 ward with three beds and two patients. ``LT-P4-<RUN> Stay`` is admitted,
transferred and discharged inside ``bed_identity_across_stay``; ``LT-P4-<RUN> Care`` is
admitted once and stays admitted for the ward board, medication chart and nursing
checks. Admit, transfer and discharge are the ADT group's, through ``ctx.client()``. The two
drug orders (one scheduled, one as needed) are placed by a raw encounter push, because
the record client's prescription write charts outpatient orders of quantity 1.

Shared-box safety:
- Only this run's ``LT-P4-`` ward, beds, bed type, patients and probe user are written.
- Cleanup runs at the end of ``run_checks`` whatever happened: the probe user is
  retired and its person voided, the patients are voided (bed unassigned and visits
  ended first, which voids their encounters, orders and obs with them), then the beds
  are deleted by id (the uuid is read from the ward layout at cleanup time, because
  Bahmni gives a bed a new uuid when a patient leaves it), the ward is retired and the
  bed type is deleted only when this run created it.
"""

from __future__ import annotations

import json
import math
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from adt_checks import (
    PRIVILEGE,
    REST,
    RUN,
    AdtCtx,
    Raw,
    Result,
    _cleanup,
    _one,
    err,
    inconclusive,
    passed,
    record,
    scrub,
)

from sm_common.integrations.adapters.bahmni_ipd_care import (
    _PERFORMER_FUNCTION,
    BahmniIpdCareClient,
    NursingConfig,
    _slot_time,
)
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    AuthError,
    ConfigError,
    HmsAdapterError,
    VendorRejected,
)
from sm_common.integrations.ipd_care_types import (
    IntakeOutputWrite,
    MarAdministrationWrite,
    MarScheduleWrite,
    MarSlot,
    VitalsWrite,
)
from sm_common.integrations.record_types import RecordAddress, RecordBed, RecordPatientInput

TAG = "LT-P4-"
NAME = f"{TAG}{RUN}"
# Bahmni's bed number column is 10 characters; a prefix apart from the ADT group's.
BEDP = f"N{RUN[:4]}"
SCHEDULE = "ipd/schedule/type/medication"
FLEXIBLE_DOSING = (
    "org.openmrs.module.bahmniemrapi.drugorder.dosinginstructions.FlexibleDosingInstructions"
)
# Stock Bahmni concept names; ``harness.ipd`` overrides them.
DRUG_NAMES = {"dose_units": "Tablet(s)", "route": "Oral", "frequency": "Twice a day"}
# The scheduled order: ceil(quantity / dose) = 3 slots, whatever is sent.
SCHEDULED_QUANTITY, SCHEDULED_DOSE = 3, 1
MODULES = ("ipd", "bedmanagement", "medicationadministration", "fhir2extension")
VITALS_SAMPLE: dict[str, int | Decimal] = {
    "pulse": 72,
    "bp_systolic": 118,
    "bp_diastolic": 76,
    "temp_c": Decimal("36.8"),
    "spo2": 97,
    "resp_rate": 16,
    "weight_kg": Decimal("61.5"),
    "height_cm": Decimal("162"),
}
IO_DATATYPES = ("Text", "Numeric", "Datetime")
IO_SET_REP = "custom:(setMembers:(uuid,display,datatype:(display)))"
ORDER_START_REP = "custom:(uuid,dateActivated,scheduledDate)"

CheckResult = tuple[str, str, Result]
IpdCheck = Callable[[AdtCtx], Awaitable[Result]]


def _nursing(x: AdtCtx) -> NursingConfig:
    return NursingConfig.from_mapping(x.h.get("nursing"))


def _ipd(x: AdtCtx) -> BahmniIpdCareClient:
    return BahmniIpdCareClient(x.http, _nursing(x))


def _drug_names(x: AdtCtx) -> dict[str, str]:
    given = x.h.get("ipd") or {}
    return {k: str(given.get(k) or v) for k, v in DRUG_NAMES.items()}


def _name(v: Any) -> str | None:
    """A concept named in a module answer: a string, or an object's display/name."""
    if isinstance(v, str):
        return v or None
    if isinstance(v, dict):
        for key in ("display", "name"):
            got = _name(v.get(key))
            if got:
                return got
    return None


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _forbidden(got: Raw, what: str) -> None:
    """A raw call's 403 is a privilege finding (INCONCLUSIVE with its name), not a FAIL."""
    if got.status == 403:
        raise AuthError(f"{what}: HTTP 403 forbidden")


# ─── synthetic data ──────────────────────────────────────────────────────────


async def _patient(x: AdtCtx, role: str) -> str:
    label = f"{NAME} {role}"
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
    return rec.uuid


async def _ward(x: AdtCtx) -> tuple[str, list[RecordBed]]:
    """This run's 1x3 ward with three beds (created once)."""
    if "ipd_ward" in x.state:
        got: tuple[str, list[RecordBed]] = x.state["ipd_ward"]
        return got
    ward = await x.record.create_ward(
        name=f"{NAME} Ward", parent_location_uuid=x.config.visit_location_uuid, rows=1, columns=3
    )
    x.later(3, "ward", f"{NAME} Ward", lambda: x._delete(f"location/{ward.uuid}"))
    wanted = f"{NAME}-IPD"
    # Only a type this run creates is ever deleted (bed types have no retire).
    before = (await x.raw("GET", "bedtype", params={"name": wanted})).results()
    bed_type = await x.record.ensure_bed_type(name=wanted, display_name=f"P4{RUN[:4]}")
    after = (await x.raw("GET", "bedtype", params={"name": wanted})).results()
    made = next((r.get("uuid") for r in after if r.get("name") == wanted), None)
    if not before and isinstance(made, str):
        x.later(4, "bed type", bed_type, lambda: x._delete(f"bedtype/{made}"))
    else:

        async def keep() -> str | None:
            return "SKIPPED reused or unidentified, not ours: left in place"

        x.later(4, "bed type", bed_type, keep)
    beds: list[RecordBed] = []
    for col in (1, 2, 3):
        bed = await x.record.add_bed(
            ward_uuid=ward.uuid, bed_number=f"{BEDP}B{col}", bed_type=bed_type, row=1, column=col
        )
        # By id: the uuid from create is gone once a patient has left the bed.
        x.later(1, "bed", bed.bed_number, lambda b=bed: x._delete_bed(b.bed_id, ward.uuid))
        beds.append(bed)
    x.state["ipd_ward"] = (ward.uuid, beds)
    return ward.uuid, beds


async def _care(x: AdtCtx) -> tuple[str, str, RecordBed]:
    """(patient, inpatient visit, bed) of the patient that stays admitted."""
    if "ipd_care" in x.state:
        got: tuple[str, str, RecordBed] = x.state["ipd_care"]
        return got
    _, beds = await _ward(x)
    patient = await _patient(x, "Care")
    admitted = await x.client(admit_new_ipd_visit=True).admit(x.adt(patient, beds[2], None))
    x.state["ipd_care"] = (patient, admitted.visit_uuid, beds[2])
    return x.state["ipd_care"]


async def _drug_order(x: AdtCtx, patient: str, visit: str, label: str, as_needed: bool) -> str:
    """One inpatient drug order placed by an encounter push; its order uuid."""
    missing = [
        k
        for k in (
            "drug_other_concept_uuid",
            "drug_quantity_units",
            "consultation_encounter_type_uuid",
        )
        if not getattr(x.config, k)
    ]
    if missing:
        raise ConfigError(f"record config missing {','.join(missing)}")
    names = _drug_names(x)
    encounter = str(uuid4())
    body = {
        "encounterUuid": encounter,
        "patientUuid": patient,
        "visitUuid": visit,
        "locationUuid": x.config.location_uuid,
        "encounterTypeUuid": x.config.consultation_encounter_type_uuid,
        "providers": [{"uuid": x.provider}],
        "observations": [],
        "drugOrders": [
            {
                "careSetting": "INPATIENT",
                "drugNonCoded": f"{NAME} {label}",
                "concept": {"uuid": x.config.drug_other_concept_uuid},
                "dosingInstructionType": FLEXIBLE_DOSING,
                "dosingInstructions": {
                    "dose": SCHEDULED_DOSE,
                    "doseUnits": names["dose_units"],
                    "route": names["route"],
                    "frequency": names["frequency"],
                    "asNeeded": as_needed,
                    "quantity": SCHEDULED_QUANTITY,
                    "quantityUnits": x.config.drug_quantity_units,
                    "administrationInstructions": json.dumps({"instructions": f"{NAME} harness"}),
                },
                "duration": 2,
                "durationUnits": "Days",
            }
        ],
    }
    pushed = await x.raw("POST", "bahmnicore/bahmniencounter", body=body)
    _forbidden(pushed, "drug order push")
    if not pushed.ok:
        raise RuntimeError(f"drug order push HTTP {pushed.status}")
    # Order fields only: the encounter's order list is a generic Order rep.
    got = await x.raw("GET", f"encounter/{encounter}", params={"v": "custom:(uuid,orders:(uuid))"})
    _forbidden(got, "drug order read-back")
    orders = [o["uuid"] for o in (got.body or {}).get("orders") or [] if isinstance(o, dict)]
    if len(orders) != 1:
        raise RuntimeError(f"drug order push left {len(orders)} orders on its encounter")
    uuid: str = orders[0]
    return uuid


async def _order_rows(x: AdtCtx, patient: str, order: str, service: str) -> list[dict[str, Any]]:
    """The module's raw by-order slot answer (for fields the client does not surface)."""
    got = await x.raw(
        "GET",
        SCHEDULE,
        params={"patientUuid": patient, "orderUuids": order, "serviceType": service},
    )
    _forbidden(got, "slot read")
    if not got.ok or not isinstance(got.body, list):
        raise RuntimeError(f"slot read HTTP {got.status}")
    return [r for r in got.body if isinstance(r, dict)]


# ─── checks ──────────────────────────────────────────────────────────────────


async def ipd_module_versions(x: AdtCtx) -> Result:
    got = await x.raw("GET", "module", params={"v": "custom:(uuid,version)"})
    if not got.ok:
        return passed(False, f"module list {got.shape()}")
    found = {
        str(r.get("uuid")).lower().replace("-", "").replace(".", ""): r.get("version")
        for r in got.results()
    }
    parts = []
    for mod in MODULES:
        ids = sorted(k for k in found if mod in k)
        exact = mod if mod in found else (ids[0] if len(ids) == 1 else None)
        parts.append(f"{mod}={found.get(exact) if exact else None}")
        if exact and exact != mod:
            parts.append(f"(id {exact})")
    return record(" ".join(parts))


async def bed_identity_across_stay(x: AdtCtx) -> Result:
    ward, beds = await _ward(x)
    first, second = beds[0], beds[1]
    ipd = _ipd(x)
    patient = await _patient(x, "Stay")
    client = x.client(admit_new_ipd_visit=True)

    async def bed(bed_id: int) -> Any:
        layout = await ipd.ward_layout(ward)
        return next((b for b in (layout.beds if layout else ()) if b.bed_id == bed_id), None)

    admitted = await client.admit(x.adt(patient, first, None))
    at_admit = await bed(first.bed_id)
    await client.transfer(x.adt(patient, second, admitted.visit_uuid))
    after_transfer, second_held = await bed(first.bed_id), await bed(second.bed_id)
    await client.discharge(x.adt(patient, None, admitted.visit_uuid), end_visit=True)
    first_end, second_end = await bed(first.bed_id), await bed(second.bed_id)
    if None in (at_admit, after_transfer, second_held, first_end, second_end):
        return passed(False, "a bed of this run was missing from the layout by bed_id")
    occupied = patient in at_admit.patient_uuids and patient in second_held.patient_uuids
    kept = (
        first_end.bed_number == first.bed_number
        and second_end.bed_number == second.bed_number
        and first_end.status == second_end.status == "AVAILABLE"
        and not first_end.patient_uuids
        and not second_end.patient_uuids
    )
    return passed(
        occupied and kept,
        f"occupied_by_bed_id={occupied} bed_id_kept={kept} "
        f"record uuid_changed_after_transfer={after_transfer.bed_uuid != at_admit.bed_uuid} "
        f"uuid_changed_after_discharge={second_end.bed_uuid != second_held.bed_uuid} "
        f"type_cleared={after_transfer.bed_type is None}",
    )


async def ward_layout_occupants(x: AdtCtx) -> Result:
    ward, _ = await _ward(x)
    patient, visit, bed = await _care(x)
    ipd = _ipd(x)
    layout = await ipd.ward_layout(ward)
    beds = layout.beds if layout else ()
    on_bed = next((b for b in beds if b.bed_id == bed.bed_id), None)
    right_bed = on_bed is not None and patient in on_bed.patient_uuids
    elsewhere = sum(1 for b in beds if b.bed_id != bed.bed_id and patient in b.patient_uuids)
    status = on_bed.status if on_bed else None
    rows = await ipd.ward_patients(ward, limit=200)
    row = next((r for r in rows if r.patient_uuid == patient), None)
    listed = row is not None and row.bed_id == bed.bed_id and row.visit_uuid == visit
    return passed(
        right_bed and status == "OCCUPIED" and not elsewhere and listed,
        f"on_bed_by_id={right_bed} status={status} on_other_beds={elsewhere} "
        f"ward_patients={len(rows)} listed_with_bed_and_visit={listed}",
    )


async def mar_schedule_create(x: AdtCtx) -> Result:
    patient, visit, _ = await _care(x)
    ipd = _ipd(x)
    order = await _drug_order(x, patient, visit, "Paracetamol 500", as_needed=False)
    prn = await _drug_order(x, patient, visit, "Ondansetron 4", as_needed=True)
    t0 = _now() + timedelta(hours=1)
    first_day = (t0, t0 + timedelta(hours=6))
    day_wise = (t0 + timedelta(hours=24), t0 + timedelta(hours=30))
    # The module charts first-day, remaining-day, then day-wise, up to ceil(qty / dose).
    expected = [*first_day, *day_wise][: math.ceil(SCHEDULED_QUANTITY / SCHEDULED_DOSE)]
    out = await ipd.create_medication_schedule(
        MarScheduleWrite(
            patient_uuid=patient,
            order_uuid=order,
            provider_uuid=x.provider,
            first_day_slots=first_day,
            day_wise_slots=day_wise,
            comments=f"{NAME} harness",
        )
    )
    window = await ipd.medication_schedule(
        patient, t0 - timedelta(hours=1), t0 + timedelta(days=3), visit_uuid=visit
    )
    slots = [s for s in window if s.order_uuid == order]
    starts = [s.start_at for s in slots]
    raw = await _order_rows(x, patient, order, "MEDICATION_REQUEST")
    raw_first = min((r.get("startTime") for r in raw), default=None, key=lambda v: v or 0)
    sent = _slot_time(t0)
    scheduled_ok = (
        out.created and len(out.slot_uuids) == len(expected) and starts == expected
    ) and raw_first == sent
    if scheduled_ok:
        x.state["ipd_orders"] = (order, prn)
        x.state["ipd_slots"] = slots

    # As needed: one placeholder slot starting at the order's effective start.
    first_prn = await ipd.create_medication_schedule(
        MarScheduleWrite(
            patient_uuid=patient,
            order_uuid=prn,
            provider_uuid=x.provider,
            first_day_slots=(),
            day_wise_slots=(),
            service_type="AsNeededMedicationRequest",
        )
    )
    placeholder = await _order_rows(x, patient, prn, "AS_NEEDED_PLACEHOLDER")
    got = await x.raw("GET", f"order/{prn}", params={"v": ORDER_START_REP})
    body = got.body if isinstance(got.body, dict) else {}
    effective = when(body.get("scheduledDate")) or when(body.get("dateActivated"))
    one = placeholder[0] if len(placeholder) == 1 else {}
    shape_ok = (
        one.get("serviceType") == "AsNeededPlaceholder"
        and one.get("startTime") is not None
        and effective is not None
        and one.get("startTime") == math.floor(effective.timestamp())
        and one.get("status") == "SCHEDULED"
    )
    writes = x.writes
    again = await ipd.create_medication_schedule(
        MarScheduleWrite(
            patient_uuid=patient,
            order_uuid=prn,
            provider_uuid=x.provider,
            first_day_slots=(),
            day_wise_slots=(),
            service_type="AsNeededMedicationRequest",
        )
    )
    repeat_ok = not again.created and x.writes == writes
    prn_ok = first_prn.created and shape_ok and repeat_ok
    return passed(
        scheduled_ok and prn_ok,
        f"created={out.created} sent={len(first_day) + len(day_wise)} charted={len(starts)} "
        f"expected={len(expected)} instants_match={starts == expected} "
        f"sent_seconds={sent} raw_startTime={raw_first} "
        f"prn_created={first_prn.created} placeholder_slots={len(placeholder)} "
        f"placeholder_shape_ok={shape_ok} prn_repeat_created={again.created} "
        f"prn_repeat_writes={x.writes - writes}",
    )


async def mar_administration_idempotent(x: AdtCtx) -> Result:
    patient, visit, _ = await _care(x)
    order, _ = x.state["ipd_orders"]
    slots: list[MarSlot] = x.state["ipd_slots"]
    ipd = _ipd(x)
    names = _drug_names(x)
    at = _now()

    def write(slot: MarSlot, status: str, reason: str | None = None) -> MarAdministrationWrite:
        return MarAdministrationWrite(
            patient_uuid=patient,
            visit_uuid=visit,
            encounter_uuid=None,
            order_uuid=order,
            slot_uuid=slot.slot_uuid,
            provider_uuids=(x.provider,),
            status=status,  # type: ignore[arg-type]
            status_reason=reason,
            administered_at=at,
            dose=Decimal(SCHEDULED_DOSE),
            dose_units=names["dose_units"],
            route=names["route"],
            notes=f"{NAME} harness dose",
            client_ref=uuid4(),
        )

    w = write(slots[0], "completed")
    first = await ipd.record_administration(w)
    writes = x.writes
    second = await ipd.record_administration(w)
    retry_ok = (
        first.created
        and first.ref_matched
        and not second.created
        and second.ref_matched
        and second.administration_uuid == first.administration_uuid
        and x.writes == writes
    )
    raw = await _order_rows(x, patient, order, "MEDICATION_REQUEST")
    row = next((r for r in raw if r.get("uuid") == slots[0].slot_uuid), {})
    admin = row.get("medicationAdministration") or {}
    providers = admin.get("providers") or [{}]
    dose_units, route = _name(admin.get("doseUnits")), _name(admin.get("route"))
    function = _name(providers[0].get("function")) if isinstance(providers[0], dict) else None
    named = {
        "doseUnits": (dose_units, names["dose_units"]),
        "route": (route, names["route"]),
        "function": (function, _PERFORMER_FUNCTION),
    }
    names_ok = all(got and got.casefold() == want.casefold() for got, want in named.values())
    shown = " ".join(
        f"{k}={got}" + ("" if got else f" (provisioning must create the concept '{want}')")
        for k, (got, want) in named.items()
    )
    window = await ipd.medication_schedule(
        patient, at - timedelta(hours=1), at + timedelta(days=3), visit_uuid=visit
    )
    back = next((s for s in window if s.slot_uuid == slots[0].slot_uuid), None)
    at_ok = back is not None and back.administered_at == at
    # One dose recorded as stopped must not stop the order.
    await ipd.record_administration(write(slots[1], "stopped", f"{NAME} harness stop"))
    code = ""
    try:
        later_ok = (await ipd.record_administration(write(slots[2], "completed"))).created
    except VendorRejected as exc:
        later_ok, code = False, f" refused={exc.code}"
    return passed(
        retry_ok and names_ok and at_ok and later_ok,
        f"first_created={first.created} retry_created={second.created} "
        f"retry_ref_matched={second.ref_matched} "
        f"same_administration={second.administration_uuid == first.administration_uuid} "
        f"retry_writes={x.writes - writes} {shown} administered_at_matches={at_ok} "
        f"later_slot_after_stopped_records={later_ok}{code}",
    )


async def mar_adhoc_marker(x: AdtCtx) -> Result:
    patient, visit, _ = await _care(x)
    _, prn = x.state["ipd_orders"]
    ipd = _ipd(x)
    names = _drug_names(x)
    w = MarAdministrationWrite(
        patient_uuid=patient,
        visit_uuid=visit,
        encounter_uuid=None,
        order_uuid=prn,
        slot_uuid=None,
        provider_uuids=(x.provider,),
        status="completed",
        status_reason=None,
        administered_at=_now(),
        dose=Decimal(SCHEDULED_DOSE),
        dose_units=names["dose_units"],
        route=names["route"],
        notes=f"{NAME} harness as-needed dose",
        client_ref=uuid4(),
    )
    marker = f"sm:{w.client_ref}"
    first = await ipd.record_adhoc_administration(w)
    # The by-order read the client uses; the visit medication read lists orderless doses only.
    doses = await _order_rows(x, patient, prn, "AS_NEEDED_MEDICATION_REQUEST")
    mine = next(
        (
            (r.get("medicationAdministration") or {})
            for r in doses
            if (r.get("medicationAdministration") or {}).get("uuid") == first.administration_uuid
        ),
        {},
    )
    texts = [n.get("text") for n in mine.get("notes") or [] if isinstance(n, dict)]
    marker_back = marker in texts
    writes = x.writes
    second = await ipd.record_adhoc_administration(w)
    after = await _order_rows(x, patient, prn, "AS_NEEDED_MEDICATION_REQUEST")
    ok = (
        first.created
        and first.ref_matched
        and marker_back
        and not second.created
        and second.ref_matched
        and x.writes == writes
        and len(after) == len(doses)
    )
    return passed(
        ok,
        f"first_created={first.created} marker_returned={marker_back}"
        + ("" if marker_back else " (the client's duplicate guard depends on it)")
        + f" retry_created={second.created} retry_ref_matched={second.ref_matched} "
        f"retry_writes={x.writes - writes} dose_slots_before={len(doses)} after={len(after)}",
    )


async def vitals_readback(x: AdtCtx) -> Result:
    # The write pre-read relies on an unknown encounter answering 404.
    unknown = await x.raw("GET", f"encounter/{uuid4()}", params={"v": "custom:(uuid)"})
    if unknown.status != 404:
        return passed(
            False, f"unknown_encounter_status={unknown.status} (the write pre-read needs 404)"
        )
    cfg = _nursing(x)
    sample = {k: v for k, v in VITALS_SAMPLE.items() if cfg.vitals_concepts.get(k)}
    if not sample:
        return passed(False, "nursing config has no vitals_concepts")
    patient, visit, _ = await _care(x)
    ipd = _ipd(x)
    ref = uuid4()
    kw: dict[str, Any] = {
        "patient_uuid": patient,
        "visit_uuid": visit,
        "provider_uuid": x.provider,
        "location_uuid": x.config.location_uuid,
        "recorded_at": _now(),
        "client_ref": ref,
        "vitals": VitalsWrite(**sample),  # type: ignore[arg-type]
    }
    out = await ipd.write_vitals(**kw)
    matched, named = 0, 0
    for key, value in sample.items():
        got = await x.raw(
            "GET",
            "obs",
            params={
                "patient": patient,
                "concept": cfg.vitals_concepts[key],
                "v": "custom:(uuid,value,voided,concept:(display),encounter:(uuid))",
            },
        )
        rows = [
            r
            for r in got.results()
            if (r.get("encounter") or {}).get("uuid") == str(ref) and r.get("voided") is not True
        ]
        if len(rows) == 1 and Decimal(str(rows[0].get("value"))) == Decimal(str(value)):
            matched += 1
            named += bool(_name(rows[0].get("concept")))

    async def top_obs() -> list[dict[str, Any]]:
        enc = await x.raw(
            "GET", f"encounter/{ref}", params={"v": "custom:(uuid,obs:(uuid,comment,voided))"}
        )
        body = enc.body if isinstance(enc.body, dict) else {}
        return [o for o in body.get("obs") or [] if isinstance(o, dict)]

    listed = await top_obs()
    marker_kept = any(o.get("comment") == f"sm:{ref}" for o in listed)
    clean = len(listed) == len(sample) and not any(o.get("voided") for o in listed)
    writes = x.writes
    again = await ipd.write_vitals(**kw)
    relisted = await top_obs()
    retry_ok = not again.created and x.writes == writes and len(relisted) == len(listed)
    return passed(
        out.created
        and matched == len(sample)
        and named == matched
        and marker_kept
        and clean
        and retry_ok,
        f"vitals={len(sample)} read_back_by_concept={matched} concept_named={named} "
        f"unknown_encounter_status=404 encounter_obs={len(listed)} marker_kept={marker_kept} "
        f"retry_created={again.created} retry_writes={x.writes - writes} "
        f"obs_after_retry={len(relisted)}",
    )


async def io_concepts_present(x: AdtCtx) -> Result:
    cfg = _nursing(x)
    keys = ("intake_concept_set_uuid", "output_concept_set_uuid")
    missing = [k for k in keys if not getattr(cfg, k)]
    if missing:
        return passed(False, f"nursing config missing {','.join(missing)}")
    gaps: list[str] = []
    datetime_member: dict[str, str] = {}
    for key in keys:
        got = await x.raw("GET", f"concept/{getattr(cfg, key)}", params={"v": IO_SET_REP})
        members = (got.body or {}).get("setMembers") if isinstance(got.body, dict) else None
        rows = [m for m in members or [] if isinstance(m, dict)]
        kinds = Counter(_name(m.get("datatype")) for m in rows)
        lacking = [t for t in IO_DATATYPES if kinds.get(t) != 1]
        if not got.ok or lacking:
            gaps.append(
                f"{key.split('_')[0]} set {got.status} needs one each of {','.join(lacking)}"
            )
        found = next((m.get("uuid") for m in rows if _name(m.get("datatype")) == "Datetime"), None)
        if isinstance(found, str):
            datetime_member[key] = found
    if gaps:
        return passed(False, "provisioning must create: " + "; ".join(gaps))
    patient, visit, _ = await _care(x)
    ref, taken = uuid4(), _now() - timedelta(minutes=5)
    try:
        await _ipd(x).write_intake_output(
            patient_uuid=patient,
            visit_uuid=visit,
            provider_uuid=x.provider,
            location_uuid=x.config.location_uuid,
            recorded_at=_now(),
            client_ref=ref,
            entry=IntakeOutputWrite(
                kind="intake", route="Oral", quantity_ml=200, recorded_at=taken
            ),
        )
    except (HmsAdapterError, ConfigError) as exc:
        return passed(
            False,
            f"intake write refused ({err(exc)}): switch _obs_time to the record's date string",
        )
    enc = await x.raw(
        "GET",
        f"encounter/{ref}",
        params={"v": "custom:(uuid,obs:(uuid,groupMembers:(value,concept:(uuid))))"},
    )
    body = enc.body if isinstance(enc.body, dict) else {}
    members = [
        m
        for o in body.get("obs") or []
        if isinstance(o, dict)
        for m in o.get("groupMembers") or []
        if isinstance(m, dict)
    ]
    dt = next(
        (
            m.get("value")
            for m in members
            if (m.get("concept") or {}).get("uuid") == datetime_member.get(keys[0])
        ),
        None,
    )
    back = when(dt)
    return passed(
        back == taken,
        f"sets_complete=2 group_members={len(members)} datetime_read_back={back is not None} "
        f"datetime_matches={back == taken}",
    )


async def mar_privileges(x: AdtCtx) -> Result:
    found: dict[str, str] = x.state.get("ipd_403", {})
    if found:
        routes = ",".join(sorted(found))
        privileges = ",".join(sorted(set(found.values())))
        return inconclusive(f"HTTP 403 on {routes}: missing privilege ({privileges})")
    patient, _, _ = await _care(x)
    ward, _ = await _ward(x)
    ipd = _ipd(x)
    # Each read raises AuthError on a 403, which reports the privilege Bahmni names.
    now = _now()
    await ipd.medication_schedule(patient, now - timedelta(days=1), now + timedelta(days=3))
    await ipd.ward_patients(ward, limit=1)
    return passed(True, f"ipd_calls={x.state.get('ipd_calls', 0) + 2} forbidden=0")


async def ward_endpoints_auth(x: AdtCtx) -> Result:
    if x.admin_http is None:
        return "SKIP", "admin login not enabled: no unprivileged probe user"
    ward, _ = await _ward(x)
    person = await x.raw(
        "POST",
        "person",
        body={
            "names": [{"givenName": NAME, "familyName": "Probe"}],
            "gender": "F",
            "birthdate": "1985-01-01",
        },
        admin=True,
    )
    person_uuid = person.body.get("uuid") if isinstance(person.body, dict) else None
    if not person.ok or not isinstance(person_uuid, str):
        return inconclusive(f"probe person not created ({person.shape()})")
    x.later(0, "probe person", NAME, lambda: x._delete(f"person/{person_uuid}"))
    username = f"lt-p4-{RUN.lower()}"
    # Per run and never printed; the user is retired at cleanup.
    password = secrets.token_urlsafe(12) + "Aa1"
    user = await x.raw(
        "POST",
        "user",
        body={"username": username, "password": password, "person": person_uuid, "roles": []},
        admin=True,
    )
    user_uuid = user.body.get("uuid") if isinstance(user.body, dict) else None
    if not user.ok or not isinstance(user_uuid, str):
        return inconclusive(f"probe user not created ({user.shape()})")
    x.later(0, "probe user", username, lambda: x._delete(f"user/{user_uuid}"))
    resp = await x.http._send(
        "GET",
        f"{REST}/ipd/wards/{ward}/patients",
        params={"offset": "0", "limit": "1"},
        auth=httpx.BasicAuth(username, password),
        # A fresh session: never the API login's cookie.
        headers={"Cookie": f"JSESSIONID={NAME}"},
    )
    hit = PRIVILEGE.search(resp.text) if resp.status_code == 403 else None
    privilege = hit.group(1).strip() if hit else None
    return record(f"unprivileged_user status={resp.status_code} privilege={privilege}")


# (name, topic, check): run in this order; later checks build on earlier state.
# Wire this group through run_checks, not by looping over CHECKS: cleanup and the
# privilege tally run only in run_checks.
CHECKS: list[tuple[str, str, IpdCheck]] = [
    ("ipd_module_versions", "ipd_module_versions", ipd_module_versions),
    ("bed_identity_across_stay", "bed_identity", bed_identity_across_stay),
    ("ward_layout_occupants", "ward_board", ward_layout_occupants),
    ("mar_schedule_create", "medication_chart_create", mar_schedule_create),
    ("mar_administration_idempotent", "medication_administration", mar_administration_idempotent),
    ("mar_adhoc_marker", "as_needed_dose", mar_adhoc_marker),
    ("vitals_readback", "nursing_vitals", vitals_readback),
    ("io_concepts_present", "intake_output", io_concepts_present),
    ("mar_privileges", "medication_chart_privileges", mar_privileges),
    ("ward_endpoints_auth", "ward_patients_auth", ward_endpoints_auth),
]


def _tally(x: AdtCtx, calls_before: int) -> None:
    """Count this check's inpatient-module calls and note every 403 among them."""
    new = min(x.calls - calls_before, len(x.tap))
    for _method, path, status, body in x.tap[len(x.tap) - new :] if new else []:
        if "/ipd/" not in path:
            continue
        x.state["ipd_calls"] = x.state.get("ipd_calls", 0) + 1
        if status == 403:
            hit = PRIVILEGE.search(json.dumps(body) if body is not None else "")
            route = scrub(path.removeprefix(REST))
            x.state.setdefault("ipd_403", {})[route] = (
                hit.group(1).strip() if hit else "not named by Bahmni"
            )


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
            before = ctx.calls
            status, detail = await _one(ctx, name, fn)
            if name != "ward_endpoints_auth":
                _tally(ctx, before)
            rows.append((name, topic, (status, scrub(detail))))
    finally:
        try:
            done, total = await _cleanup(ctx)
            ctx.state["ipd_cleanup"] = (done, total)
            if done < total:
                print(
                    f"CLEANUP INCOMPLETE {total - done} left; find them by the name {NAME}",
                    flush=True,
                )
        finally:
            ctx.cleanups = prior
    return rows
