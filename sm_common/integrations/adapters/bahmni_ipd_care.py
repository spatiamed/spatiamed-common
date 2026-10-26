"""Bahmni inpatient care client: ward board, nursing chart, medication chart.

A standalone client over the shared ``BahmniHttp`` (auth, re-login on 401, error
mapping). It makes no retries; every write takes a caller ``client_ref`` and has a
``find_*`` partner so a caller can retry safely.

Ward board
    ``GET admissionLocation/<ward>?v=full`` answers ``bedLayouts``: one entry per
    layout cell (rowNumber, columnNumber, bedNumber, bedId, bedUuid, status,
    bedType, patients, bedTagMaps). A cell with ``bedId: null`` is a gap in the
    grid, not a bed. Beds are keyed on ``bedId``: bed management gives a bed a new
    uuid and clears its type whenever a patient leaves it, so ``bed_uuid`` is
    reported as of the read only. Bahmni answers an unknown ward uuid with HTTP 500;
    the core ``location/<uuid>`` route is asked to tell that apart from a server
    fault. A 404 on ``admissionLocation`` itself means the route is missing.

Ward patients
    ``GET ipd/wards/<ward>/patients?offset=&limit=`` (bahmni-ipd) lists patients
    with an open bed assignment and an active visit in the ward.

Nursing observations
    ``POST bahmnicore/bahmniencounter`` with the configured nursing encounter type and
    plain obs by concept (not form-namespaced). Each write is its own encounter: it
    names ``encounterUuid = str(client_ref)``, because a write naming no encounter is
    merged into a matching one and fails once a visit holds two encounters of one type
    and provider. So the hospital-record encounter uuid of a write is its
    ``client_ref``, one encounter per write. Before posting, every write reads its own
    encounter (``GET encounter/<client_ref>``): when it exists in the same visit the
    earlier write is returned (``created=False``) and nothing is sent, so a retry after
    an unknown outcome (a timeout, a gateway 5xx after the commit) never charts the obs
    twice; an encounter of that uuid in another visit is refused before any write.
    The encounter's existence is the proof, not its obs: re-sending the same encounter
    uuid would add the obs again, even after someone voided the earlier ones.
    ``find_write_by_ref`` is that same read. The first obs also carries the comment
    ``"sm:" + str(client_ref)`` so the write is recognisable in the record. An
    intake/output date-time member is sent as epoch ms, like every instant here
    (``_obs_time``). Values are checked against data-entry limits before any I/O.

Medication chart
    openmrs-module-ipd (1.1.1) keeps one schedule per visit with one slot per due
    dose. Its wire instants are epoch SECONDS (UTC), not ms: the module converts with
    ``Instant.ofEpochSecond`` / ``toEpochSecond`` (``DateTimeUtil``) for the
    ``startTime``/``endTime`` read params, the slot arrays sent to create a schedule
    and each slot's ``startTime`` in an answer. ``_slot_time`` / ``_slot_instant`` are
    the only places that know this. A slot's administration carries
    ``administeredDateTime`` as a java ``Date`` (epoch ms), parsed like every other
    record date and refused when it does not parse or falls before 2000 (a seconds
    value would read as 1970).
    ``GET ipd/schedule/type/medication?patientUuid&startTime&endTime[&visitUuid]``
    answers schedules with their ``slots``; ``?patientUuid&orderUuids&serviceType``
    answers a flat slot list for one order. ``serviceType`` there binds to the
    module's enum by name, and when it is absent the module reads scheduled slots
    only, so the read for an as-needed order must name ``AS_NEEDED_PLACEHOLDER``; a
    slot of another type in the answer is refused (the filter was not applied).
    ``POST ipd/schedule/type/medication`` charts an order against the patient's
    active visit; its answer names no slots, so they are read back by order, and an
    empty read-back is refused (nothing was charted). The client reads the order's
    slots first and sends nothing when the order is on the chart already: the module
    refuses a second scheduled chart only with a 400 and does not refuse a second
    as-needed placeholder at all. ``serviceType`` is sent as the enum name
    (``MEDICATION_REQUEST``, ``AS_NEEDED_PLACEHOLDER``) and answered as its concept
    name (``MedicationRequest``). ``medicationFrequency`` is
    ``FIXED_SCHEDULE_FREQUENCY``, the mode that charts the three slot arrays as
    given; ``slotStartTime`` is read only in the other mode and is sent as the
    earliest slot for reference. An as-needed order is charted as one placeholder
    slot that starts at the order's start (the module charts nothing for
    ``AS_NEEDED_MEDICATION_REQUEST``). Reading needs the ``Get Medication Tasks`` and
    ``Get Medication Administration`` privileges and charting ``Edit Medication
    Tasks``; the module answers 403 without them (``AuthError``).

Administrations
    ``POST ipd/scheduledMedicationAdministrations`` takes a list of requests, one per
    slot, and answers the slot's existing administration when it already has one
    (nothing new is saved). A request ``uuid`` makes the module answer that
    administration instead of creating one, so none is sent. The request's
    ``administeredDateTime`` is epoch SECONDS (``_slot_time``); the answer's is a java
    date. ``statusReason`` is accepted but not saved by the module, so a not-done reason
    also travels as a note. ``doseUnits``, ``route`` and the performer ``function`` are
    resolved by concept name. Each write carries the note ``"sm:" + str(client_ref)``;
    notes come back unordered, so any note equal to it matches.
    The client reads the order's slots first (``order_not_active`` on a stopped order,
    the slot's existing administration returned with ``created=False``), and after the
    POST reads the slot back: the administration it holds is the outcome, and its notes
    tell our write from one recorded by someone else in between.
    ``POST ipd/adhocMedicationAdministrations`` (an as-needed dose) always creates: the
    module files it as a new completed ``AsNeededMedicationRequest`` slot of the order
    in the patient's active visit. It is not idempotent, so the client first reads the
    order's as-needed dose slots (``serviceType=AS_NEEDED_MEDICATION_REQUEST``) for its
    note and sends nothing when an earlier write is found. The visit medication read
    (``ipdVisit/<visit>/medication``) lists only doses with no order, so it cannot see
    these. The order's chart placeholder is read too, as the evidence that the order was
    stopped before its first as-needed dose.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast, get_args
from uuid import UUID

from sm_common.integrations.adapters.bahmni_http import BahmniHttp, from_epoch_ms, to_epoch_ms
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.ipd_care_types import (
    AdministrationStatus,
    BedStatus,
    IntakeOutputWrite,
    MarAdministrationOutcome,
    MarAdministrationWrite,
    MarScheduleOutcome,
    MarScheduleWrite,
    MarSlot,
    NursingWriteOutcome,
    SlotServiceType,
    SlotStatus,
    VitalsWrite,
    WardBed,
    WardLayout,
    WardPatient,
)

_REST = "/openmrs/ws/rest/v1"
_WARDS = f"{_REST}/admissionLocation"
_LOCATION = f"{_REST}/location"
_CONCEPT = f"{_REST}/concept"
_IPD_WARDS = f"{_REST}/ipd/wards"
_VISIT = f"{_REST}/visit"
_ENCOUNTER = f"{_REST}/bahmnicore/bahmniencounter"
_ENCOUNTER_READ = f"{_REST}/encounter"
_MARKER_PREFIX = "sm:"
_EARLIER_WRITE_REP = "custom:(uuid,visit:(uuid),obs:(uuid,comment))"
_IO_SET_REP = "custom:(setMembers:(uuid,display,datatype:(display)))"
_MAX_NOTE_CHARS = 4000
_MAX_IO_ROUTE_CHARS = 255
_MAX_IO_QUANTITY_ML = 10000
_CONCEPT_DATATYPE_REP = "custom:(uuid,datatype:(display))"
# OpenMRS uuids are up to 38 chars of [A-Za-z0-9-]; anything else could reach another route.
_UUID_SHAPE = re.compile(r"[A-Za-z0-9-]{1,38}")
_MAX_WARD_PATIENTS_PAGE = 200
_MEDICATION_SCHEDULE = f"{_REST}/ipd/schedule/type/medication"
_MAX_SCHEDULE_COMMENT_CHARS = 1000
# Caller service type -> the module's enum name, sent on the create and the order read.
_SCHEDULE_SERVICE_TYPE = {
    "MedicationRequest": "MEDICATION_REQUEST",
    "AsNeededMedicationRequest": "AS_NEEDED_PLACEHOLDER",
}
# The concept name the module answers for each enum name it is sent.
_SERVICE_TYPE_ANSWER = {
    "MEDICATION_REQUEST": "MedicationRequest",
    "AS_NEEDED_PLACEHOLDER": "AsNeededPlaceholder",
    "AS_NEEDED_MEDICATION_REQUEST": "AsNeededMedicationRequest",
}
_SCHEDULED_ADMINISTRATIONS = f"{_REST}/ipd/scheduledMedicationAdministrations"
_ADHOC_ADMINISTRATIONS = f"{_REST}/ipd/adhocMedicationAdministrations"
_ADMINISTRATION_STATUSES = frozenset(get_args(AdministrationStatus))
_MAX_ADMINISTRATION_NOTE_CHARS = 1000
_MAX_CONCEPT_NAME_CHARS = 255
_PERFORMER_FUNCTION = "Performer"
# Slot states that may still take an administration (a missed dose may be given late).
_RECORDABLE_SLOT_STATUSES = frozenset({"SCHEDULED", "MISSED"})
# Any given-at time before this is a unit slip (seconds read as ms), not a real dose.
_EARLIEST_ADMINISTRATION = datetime(2000, 1, 1, tzinfo=timezone.utc)
_SLOT_STATUSES = frozenset(get_args(SlotStatus))
_SLOT_SERVICE_TYPES = frozenset(get_args(SlotServiceType))

VITALS_KEYS = frozenset(
    {
        "pulse",
        "bp_systolic",
        "bp_diastolic",
        "temp_c",
        "spo2",
        "resp_rate",
        "weight_kg",
        "height_cm",
    }
)
# Data-entry limits (inclusive), not clinical normals. Order is the obs order sent.
_VITALS_LIMITS: tuple[tuple[str, Decimal, Decimal], ...] = (
    ("pulse", Decimal(20), Decimal(250)),
    ("bp_systolic", Decimal(40), Decimal(300)),
    ("bp_diastolic", Decimal(20), Decimal(200)),
    ("temp_c", Decimal("30.0"), Decimal("45.0")),
    ("spo2", Decimal(50), Decimal(100)),
    ("resp_rate", Decimal(4), Decimal(80)),
    ("weight_kg", Decimal("0.3"), Decimal(400)),
    ("height_cm", Decimal(20), Decimal(250)),
)
_DECIMAL_VITALS = frozenset({"temp_c", "weight_kg", "height_cm"})
_TEXT_FIELDS = (
    "intake_concept_set_uuid",
    "output_concept_set_uuid",
    "nursing_note_concept_uuid",
    "nursing_encounter_type_uuid",
)


@dataclass(frozen=True)
class NursingConfig:
    """Install-specific concept and encounter-type uuids; no defaults, no env reads."""

    # vitals key (see VITALS_KEYS) -> concept uuid
    vitals_concepts: Mapping[str, str] = field(default_factory=dict)
    intake_concept_set_uuid: str = ""
    output_concept_set_uuid: str = ""
    nursing_note_concept_uuid: str = ""
    nursing_encounter_type_uuid: str = ""

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> NursingConfig:
        """Build from a field mapping. Keys this client does not use are ignored;
        an unknown ``vitals_concepts`` key is a ``ConfigError`` naming it."""
        src = m or {}
        vitals_raw = src.get("vitals_concepts") or {}
        if not isinstance(vitals_raw, Mapping):
            raise ConfigError("nursing config: vitals_concepts must be a mapping")
        unknown = sorted(str(k) for k in vitals_raw if k not in VITALS_KEYS)
        if unknown:
            raise ConfigError(f"nursing config: unknown vitals_concepts keys: {', '.join(unknown)}")
        vitals: dict[str, str] = {}
        for key, value in vitals_raw.items():
            if not isinstance(value, str):
                raise ConfigError(f"nursing config: vitals_concepts.{key} must be a string")
            vitals[key] = value
        texts: dict[str, str] = {}
        for name in _TEXT_FIELDS:
            value = src.get(name, "")
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ConfigError(f"nursing config: {name} must be a string")
            texts[name] = value
        return cls(vitals_concepts=vitals, **texts)


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _optional_text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    return v.strip() or None


def _check_uuid(name: str, value: str) -> None:
    """Caller input check (ValueError) before a value enters a request path."""
    if not isinstance(value, str) or not _UUID_SHAPE.fullmatch(value):
        raise ValueError(f"bahmni: {name} is not a uuid: {value!r}")


def _marker(client_ref: UUID) -> str:
    if not isinstance(client_ref, UUID):
        raise ValueError(f"bahmni: client_ref must be a UUID: {client_ref!r}")
    return _MARKER_PREFIX + str(client_ref)


def _obs_time(dt: datetime) -> int:
    """A date-time obs value: epoch ms, the module's one wire encoding for instants."""
    return to_epoch_ms(dt)


def _slot_time(dt: datetime) -> int:
    """An instant on the medication chart wire: epoch seconds (naive -> ValueError)."""
    if not isinstance(dt, datetime):
        raise ValueError(f"bahmni: slot time must be a datetime: {dt!r}")
    return to_epoch_ms(dt) // 1000


def _slot_instant(raw: Any) -> datetime:
    """A medication chart ``startTime`` (epoch seconds) -> aware UTC."""
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise VendorRejected(f"bahmni: medication slot time was not epoch seconds: {raw!r}")
    try:
        seconds = int(raw.strip()) if isinstance(raw, str) else int(raw)
        return from_epoch_ms(seconds * 1000)
    except (ValueError, OverflowError) as exc:
        raise VendorRejected(
            f"bahmni: medication slot time was not epoch seconds: {raw!r}"
        ) from exc


def _administered_at(slot_uuid: str, admin: Mapping[str, Any]) -> datetime | None:
    """A slot administration's given-at time: a java date (epoch ms), not a slot instant."""
    raw = admin.get("administeredDateTime")
    if raw is None:
        return None
    at = when(raw)
    if at is None or at < _EARLIEST_ADMINISTRATION:
        raise VendorRejected(
            f"bahmni: medication slot {slot_uuid} administration time was not a plausible "
            f"date: {raw!r}"
        )
    return at


def _mar_slot(raw: Any) -> MarSlot:
    if not isinstance(raw, dict):
        raise VendorRejected("bahmni: medication slot was not an object")
    slot_uuid = _text(raw.get("uuid"))
    order = raw.get("order")
    order_uuid = _text(order.get("uuid")) if isinstance(order, dict) else ""
    if not slot_uuid or not order_uuid:
        raise VendorRejected("bahmni: medication slot missing its uuid or order uuid")
    status = raw.get("status")
    if status not in _SLOT_STATUSES:
        raise VendorRejected(f"bahmni: medication slot {slot_uuid} has unknown status {status!r}")
    service_type = raw.get("serviceType")
    if service_type not in _SLOT_SERVICE_TYPES:
        raise VendorRejected(
            f"bahmni: medication slot {slot_uuid} has unknown service type {service_type!r}"
        )
    # Only the order uuid and drug display are read: the order arrives as a full rep.
    drug = order.get("drug") if isinstance(order, dict) else None
    admin = raw.get("medicationAdministration")
    if admin is not None and not isinstance(admin, dict):
        raise VendorRejected(
            f"bahmni: medication slot {slot_uuid} administration was not an object"
        )
    return MarSlot(
        slot_uuid=slot_uuid,
        order_uuid=order_uuid,
        drug_name=_optional_text(drug.get("display")) if isinstance(drug, dict) else None,
        service_type=cast(SlotServiceType, service_type),
        start_at=_slot_instant(raw.get("startTime")),
        status=cast(SlotStatus, status),
        administration_uuid=_optional_text(admin.get("uuid")) if admin else None,
        administered_at=_administered_at(slot_uuid, admin) if admin else None,
        notes=_optional_text(raw.get("notes")),
    )


def _vital_value(name: str, value: Any, low: Decimal, high: Decimal) -> int | float:
    """Range-check one vital; the JSON number sent for it."""
    if name in _DECIMAL_VITALS:
        if isinstance(value, bool) or not isinstance(value, Decimal | int):
            raise ValueError(f"bahmni: vitals.{name} must be a Decimal: {value!r}")
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError(f"bahmni: vitals.{name} must be finite: {value!r}")
    else:
        if _int(value) is None:
            raise ValueError(f"bahmni: vitals.{name} must be an integer: {value!r}")
        number = Decimal(value)
    if not low <= number <= high:
        raise ValueError(f"bahmni: vitals.{name} must be {low}-{high}: {value!r}")
    return int(number) if number == number.to_integral_value() else float(number)


def _status(raw: Any) -> BedStatus:
    # Bed management knows only AVAILABLE and OCCUPIED; like its own status search,
    # anything other than AVAILABLE reads as OCCUPIED (never offered as free).
    return "AVAILABLE" if raw == "AVAILABLE" else "OCCUPIED"


def _tags(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise VendorRejected("bahmni: bed layout bedTagMaps was not a list")
    out: list[str] = []
    for tag_map in raw:
        tag = tag_map.get("bedTag") if isinstance(tag_map, dict) else None
        name = _text(tag.get("name")) if isinstance(tag, dict) else ""
        if name:
            out.append(name)
    return tuple(out)


def _bed(raw: dict[str, Any]) -> WardBed | None:
    """One layout cell -> a bed keyed on its id; ``None`` for a cell with no bed."""
    if raw.get("bedId") is None:
        return None
    bed_id = _int(raw.get("bedId"))
    row, column = _int(raw.get("rowNumber")), _int(raw.get("columnNumber"))
    if bed_id is None or row is None or column is None:
        raise VendorRejected("bahmni: bed layout entry missing bedId or position")
    patients = raw.get("patients") or []
    if not isinstance(patients, list):
        raise VendorRejected("bahmni: bed layout patients was not a list")
    bed_type = raw.get("bedType")
    return WardBed(
        bed_id=bed_id,
        bed_uuid=_optional_text(raw.get("bedUuid")),
        bed_number=_text(raw.get("bedNumber")),
        # The type name is what bed create takes; displayName is presentation only.
        bed_type=_optional_text(bed_type.get("name")) if isinstance(bed_type, dict) else None,
        row=row,
        column=column,
        status=_status(raw.get("status")),
        patient_uuids=tuple(
            p["uuid"] for p in patients if isinstance(p, dict) and isinstance(p.get("uuid"), str)
        ),
        tags=_tags(raw.get("bedTagMaps")),
    )


def _layout(body: Any) -> WardLayout:
    if not isinstance(body, dict):
        raise VendorRejected("bahmni: admission location response was not an object")
    ward = body.get("ward")
    if not isinstance(ward, dict):
        raise VendorRejected("bahmni: admission location missing ward")
    uuid = ward.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        raise VendorRejected("bahmni: admission location missing ward uuid")
    layouts = body.get("bedLayouts")
    if not isinstance(layouts, list):
        raise VendorRejected("bahmni: admission location carried no bedLayouts list")
    if not all(isinstance(cell, dict) for cell in layouts):
        raise VendorRejected("bahmni: bed layout entry was not an object")
    beds = sorted(
        (b for cell in layouts if (b := _bed(cell)) is not None),
        key=lambda b: (b.row, b.column, b.bed_id),
    )
    rows = [r for cell in layouts if (r := _int(cell.get("rowNumber"))) is not None]
    columns = [c for cell in layouts if (c := _int(cell.get("columnNumber"))) is not None]
    return WardLayout(
        ward_uuid=uuid,
        ward_name=_text(ward.get("display")) or _text(ward.get("name")),
        rows=max(rows, default=0),
        columns=max(columns, default=0),
        beds=tuple(beds),
    )


def _ward_patient(raw: Any) -> WardPatient:
    """One ``activePatients`` row: patientDetails, visitDetails, bedDetails."""
    if not isinstance(raw, dict):
        raise VendorRejected("bahmni: ward patient row was not an object")
    patient = raw.get("patientDetails")
    visit = raw.get("visitDetails")
    bed = raw.get("bedDetails")
    if not isinstance(patient, dict) or not isinstance(visit, dict):
        raise VendorRejected("bahmni: ward patient row missing patientDetails or visitDetails")
    patient_uuid, visit_uuid = _text(patient.get("uuid")), _text(visit.get("uuid"))
    if not patient_uuid or not visit_uuid:
        raise VendorRejected("bahmni: ward patient row missing patient or visit uuid")
    person = patient.get("person")
    name = _text(person.get("display")) if isinstance(person, dict) else ""
    return WardPatient(
        patient_uuid=patient_uuid,
        visit_uuid=visit_uuid,
        bed_id=_int(bed.get("bedId")) if isinstance(bed, dict) else None,
        bed_number=_optional_text(bed.get("bedNumber")) if isinstance(bed, dict) else None,
        admitted_at=when(visit.get("admissionDateTime")),
        display_name=name or _text(patient.get("display")),
    )


def _top_obs_uuids(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(o["uuid"] for o in raw if isinstance(o, dict) and isinstance(o.get("uuid"), str))


def _refusal(code: str, message: str) -> VendorRejected:
    exc = VendorRejected(f"{code}: {message}")
    exc.code = code
    return exc


def _order_stopped(raw_slot: Mapping[str, Any]) -> bool:
    """Positive evidence in one slot answer that its order is stopped."""
    if raw_slot.get("status") == "STOPPED":
        return True
    order = raw_slot.get("order")
    if not isinstance(order, dict):
        return False
    return order.get("dateStopped") is not None or order.get("action") == "DISCONTINUE"


def _admin_notes(admin: Mapping[str, Any]) -> list[str]:
    notes = admin.get("notes")
    if not isinstance(notes, list):
        return []
    return [n["text"] for n in notes if isinstance(n, dict) and isinstance(n.get("text"), str)]


def _admin_status(slot_uuid: str, admin: Mapping[str, Any]) -> AdministrationStatus:
    status = admin.get("status")
    if status not in _ADMINISTRATION_STATUSES:
        raise VendorRejected(
            f"bahmni: medication slot {slot_uuid} administration has unknown status {status!r}"
        )
    return cast(AdministrationStatus, status)


def _optional_name(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_CONCEPT_NAME_CHARS:
        raise ValueError(f"bahmni: {name} must be 1-{_MAX_CONCEPT_NAME_CHARS} characters")
    return value.strip()


def _administration_body(w: MarAdministrationWrite, marker: str) -> dict[str, Any]:
    """The module's administration request (no ``uuid``, no ``slotUuid``)."""
    author = w.provider_uuids[0]
    notes: list[dict[str, str]] = []
    if w.notes is not None:
        notes.append({"authorUuid": author, "text": w.notes})
    if w.status_reason is not None:
        notes.append({"authorUuid": author, "text": f"Reason: {w.status_reason.strip()}"})
    notes.append({"authorUuid": author, "text": marker})
    body: dict[str, Any] = {"patientUuid": w.patient_uuid, "orderUuid": w.order_uuid}
    if w.encounter_uuid is not None:
        body["encounterUuid"] = w.encounter_uuid
    body["providers"] = [
        {"providerUuid": p, "function": _PERFORMER_FUNCTION} for p in w.provider_uuids
    ]
    body["notes"] = notes
    body["status"] = w.status
    if w.status_reason is not None:
        body["statusReason"] = w.status_reason.strip()
    body["administeredDateTime"] = _slot_time(w.administered_at)
    if w.dose is not None:
        body["dose"] = float(w.dose)
    for key, value in (("doseUnits", w.dose_units), ("route", w.route)):
        if value is not None:
            body[key] = value.strip()
    return body


def _check_administration(w: MarAdministrationWrite) -> str:
    """Caller input checks shared by both writes (before any I/O); the marker."""
    if not isinstance(w, MarAdministrationWrite):
        raise ValueError("bahmni: w must be a MarAdministrationWrite")
    for name in ("patient_uuid", "visit_uuid", "order_uuid"):
        _check_uuid(name, getattr(w, name))
    if w.encounter_uuid is not None:
        _check_uuid("encounter_uuid", w.encounter_uuid)
    if not isinstance(w.provider_uuids, tuple) or not w.provider_uuids:
        raise ValueError("bahmni: an administration needs at least one provider")
    for provider in w.provider_uuids:
        _check_uuid("provider_uuid", provider)
    if w.status not in _ADMINISTRATION_STATUSES:
        raise ValueError(
            f"bahmni: administration status must be one of "
            f"{sorted(_ADMINISTRATION_STATUSES)}: {w.status!r}"
        )
    if w.status_reason is not None and (
        not isinstance(w.status_reason, str)
        or len(w.status_reason) > _MAX_ADMINISTRATION_NOTE_CHARS
    ):
        raise ValueError(
            f"bahmni: status_reason must be at most {_MAX_ADMINISTRATION_NOTE_CHARS} chars"
        )
    if w.status == "not-done" and (w.status_reason is None or not w.status_reason.strip()):
        raise ValueError("bahmni: a not-done administration needs a status_reason")
    if w.status_reason is not None and not w.status_reason.strip():
        raise ValueError("bahmni: status_reason must not be blank")
    _slot_time(w.administered_at)  # naive -> ValueError
    if w.dose is not None and (
        not isinstance(w.dose, Decimal) or not w.dose.is_finite() or w.dose <= 0
    ):
        raise ValueError(f"bahmni: dose must be a positive finite Decimal: {w.dose!r}")
    _optional_name("dose_units", w.dose_units)
    _optional_name("route", w.route)
    if w.notes is not None and (
        not isinstance(w.notes, str)
        or not w.notes.strip()
        or len(w.notes) > _MAX_ADMINISTRATION_NOTE_CHARS
    ):
        raise ValueError(
            f"bahmni: administration notes must be 1-{_MAX_ADMINISTRATION_NOTE_CHARS} chars"
        )
    return _marker(w.client_ref)


def _outcome(
    slot_uuid: str, admin: Mapping[str, Any], marker: str, *, created: bool
) -> MarAdministrationOutcome:
    uuid = admin.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        raise VendorRejected(f"bahmni: medication slot {slot_uuid} administration has no uuid")
    return MarAdministrationOutcome(
        administration_uuid=uuid,
        slot_uuid=slot_uuid,
        created=created,
        status=_admin_status(slot_uuid, admin),
        ref_matched=marker in _admin_notes(admin),
    )


class BahmniIpdCareClient:
    def __init__(self, http: BahmniHttp, config: NursingConfig) -> None:
        self._http = http
        self._config = config
        # Intake/output set uuid -> (route, quantity, date-time) member concept uuids.
        self._io_members: dict[str, tuple[str, str, str]] = {}

    # ─── Ward board ──────────────────────────────────────────────────────────

    async def ward_layout(self, ward_uuid: str) -> WardLayout | None:
        """The ward's bed grid with occupants and tags; ``None`` for an unknown ward."""
        _check_uuid("ward_uuid", ward_uuid)
        try:
            resp = await self._http.request("GET", f"{_WARDS}/{ward_uuid}", params={"v": "full"})
        except TransientError as original:
            if await self._location_missing(ward_uuid):
                return None
            raise original
        if resp.status_code == 404:
            raise NotFound("bahmni: admissionLocation route not found (HTTP 404)")
        return _layout(resp.json() if resp.content else None)

    async def _location_missing(self, uuid: str) -> bool:
        """True only when Bahmni's core location route says 404 for ``uuid``."""
        try:
            resp = await self._http.request(
                "GET", f"{_LOCATION}/{uuid}", params={"v": "custom:(uuid)"}
            )
        except TransientError:
            return False
        return resp.status_code == 404

    async def ward_patients(
        self, ward_uuid: str, *, offset: int = 0, limit: int = 50
    ) -> list[WardPatient]:
        """One page of the ward's admitted patients (``limit`` 1-200, ``offset`` >= 0)."""
        _check_uuid("ward_uuid", ward_uuid)
        if _int(offset) is None or offset < 0:
            raise ValueError(f"bahmni: offset must be >= 0: {offset!r}")
        if _int(limit) is None or not 1 <= limit <= _MAX_WARD_PATIENTS_PAGE:
            raise ValueError(f"bahmni: limit must be 1-{_MAX_WARD_PATIENTS_PAGE}: {limit!r}")
        resp = await self._http.request(
            "GET",
            f"{_IPD_WARDS}/{ward_uuid}/patients",
            params={"offset": str(offset), "limit": str(limit)},
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: ipd ward patients route not found (HTTP 404)")
        body = resp.json() if resp.content else None
        rows = body.get("activePatients") if isinstance(body, dict) else None
        if rows is None and isinstance(body, dict) and body.get("totalPatients") == 0:
            return []
        if not isinstance(rows, list):
            raise VendorRejected("bahmni: ward patients response carried no activePatients list")
        return [_ward_patient(r) for r in rows]

    # ─── Concepts ────────────────────────────────────────────────────────────

    async def concept_datatypes(self, concept_uuids: Sequence[str]) -> dict[str, str]:
        """Concept uuid -> datatype name (``Numeric``, ``Text``, ...); unknown -> ``NotFound``."""
        unique = list(dict.fromkeys(concept_uuids))
        for uuid in unique:
            _check_uuid("concept_uuid", uuid)
        out: dict[str, str] = {}
        for uuid in unique:
            resp = await self._http.request(
                "GET", f"{_CONCEPT}/{uuid}", params={"v": _CONCEPT_DATATYPE_REP}
            )
            if resp.status_code == 404:
                raise NotFound(f"bahmni: no concept {uuid}")
            body = resp.json() if resp.content else None
            datatype = body.get("datatype") if isinstance(body, dict) else None
            name = _text(datatype.get("display")) if isinstance(datatype, dict) else ""
            if not name:
                raise VendorRejected(f"bahmni: concept {uuid} carried no datatype")
            out[uuid] = name
        return out

    # ─── Nursing observations ────────────────────────────────────────────────

    async def write_vitals(
        self,
        *,
        patient_uuid: str,
        visit_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        recorded_at: datetime,
        client_ref: UUID,
        vitals: VitalsWrite,
    ) -> NursingWriteOutcome:
        """One vitals entry as plain obs on the configured concepts (unset fields skipped).

        Each value must be inside its data-entry limit, diastolic below systolic when
        both are given, and at least one field set (``ValueError`` before any I/O).
        """
        if not isinstance(vitals, VitalsWrite):
            raise ValueError("bahmni: vitals must be a VitalsWrite")
        observations: list[dict[str, Any]] = []
        values: dict[str, int | float] = {}
        for name, low, high in _VITALS_LIMITS:
            raw = getattr(vitals, name)
            if raw is None:
                continue
            values[name] = _vital_value(name, raw, low, high)
        if not values:
            raise ValueError("bahmni: vitals has no field set")
        sys_bp, dia_bp = values.get("bp_systolic"), values.get("bp_diastolic")
        if sys_bp is not None and dia_bp is not None and not dia_bp < sys_bp:
            raise ValueError("bahmni: vitals.bp_diastolic must be below bp_systolic")
        for name, value in values.items():
            concept = self._config.vitals_concepts.get(name, "")
            if not concept:
                raise ConfigError(f"nursing config: vitals_concepts.{name} is required")
            observations.append({"concept": {"uuid": concept}, "value": value})
        return await self._write(
            patient_uuid,
            visit_uuid,
            provider_uuid,
            location_uuid,
            recorded_at,
            client_ref,
            observations,
        )

    async def write_intake_output(
        self,
        *,
        patient_uuid: str,
        visit_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        recorded_at: datetime,
        client_ref: UUID,
        entry: IntakeOutputWrite,
    ) -> NursingWriteOutcome:
        """One intake or output entry: an obs group under the configured set with
        route (Text), quantity in ml (Numeric) and date-time (Datetime) members.

        The member concepts are read from the set once per client and matched by
        datatype; a set without exactly one member of each is
        ``ConfigError("io_concepts_incomplete")`` before the write.
        """
        if not isinstance(entry, IntakeOutputWrite):
            raise ValueError("bahmni: entry must be an IntakeOutputWrite")
        if entry.kind == "intake":
            key = "intake_concept_set_uuid"
        elif entry.kind == "output":
            key = "output_concept_set_uuid"
        else:
            raise ValueError(f"bahmni: intake/output kind must be intake or output: {entry.kind!r}")
        route = entry.route.strip() if isinstance(entry.route, str) else ""
        if not route or len(route) > _MAX_IO_ROUTE_CHARS:
            raise ValueError(f"bahmni: intake/output route must be 1-{_MAX_IO_ROUTE_CHARS} chars")
        qty = _int(entry.quantity_ml)
        if qty is None or not 0 <= qty <= _MAX_IO_QUANTITY_ML:
            raise ValueError(
                f"bahmni: quantity_ml must be 0-{_MAX_IO_QUANTITY_ML}: {entry.quantity_ml!r}"
            )
        taken_at = _obs_time(entry.recorded_at)
        set_uuid: str = getattr(self._config, key)
        if not set_uuid:
            raise ConfigError(f"nursing config: {key} is required")
        self._check_write(
            patient_uuid, visit_uuid, provider_uuid, location_uuid, recorded_at, client_ref
        )
        route_c, qty_c, time_c = await self._io_set_members(set_uuid)
        group = {
            "concept": {"uuid": set_uuid},
            "groupMembers": [
                {"concept": {"uuid": route_c}, "value": route},
                {"concept": {"uuid": qty_c}, "value": qty},
                {"concept": {"uuid": time_c}, "value": taken_at},
            ],
        }
        return await self._write(
            patient_uuid,
            visit_uuid,
            provider_uuid,
            location_uuid,
            recorded_at,
            client_ref,
            [group],
        )

    async def write_nursing_note(
        self,
        *,
        patient_uuid: str,
        visit_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        recorded_at: datetime,
        client_ref: UUID,
        text: str,
    ) -> NursingWriteOutcome:
        """A free-text nursing note (1-4,000 characters, not blank) as one text obs."""
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_NOTE_CHARS:
            raise ValueError(f"bahmni: nursing note must be 1-{_MAX_NOTE_CHARS} characters")
        concept = self._config.nursing_note_concept_uuid
        if not concept:
            raise ConfigError("nursing config: nursing_note_concept_uuid is required")
        return await self._write(
            patient_uuid,
            visit_uuid,
            provider_uuid,
            location_uuid,
            recorded_at,
            client_ref,
            [{"concept": {"uuid": concept}, "value": text}],
        )

    async def find_write_by_ref(
        self, visit_uuid: str, client_ref: UUID
    ) -> NursingWriteOutcome | None:
        """The earlier write named ``client_ref`` in this visit, else ``None``.

        One read of the encounter the write named (``encounter/<client_ref>``); the
        outcome names its top-level obs. An encounter of that uuid in another visit, or
        an answer naming another encounter, is ``VendorRejected`` (nothing written).
        """
        _check_uuid("visit_uuid", visit_uuid)
        _marker(client_ref)  # a UUID, or ValueError before any I/O
        ref = str(client_ref)
        resp = await self._http.request(
            "GET", f"{_ENCOUNTER_READ}/{ref}", params={"v": _EARLIER_WRITE_REP}
        )
        if resp.status_code == 404:
            return None
        body = resp.json() if resp.content else None
        if not isinstance(body, dict) or body.get("uuid") != ref:
            raise VendorRejected(f"bahmni: encounter read for {ref} named another encounter")
        visit = body.get("visit")
        found_visit = visit.get("uuid") if isinstance(visit, dict) else None
        if found_visit != visit_uuid:
            raise VendorRejected(
                f"bahmni: encounter {ref} already exists outside visit {visit_uuid}; "
                "client_ref reused"
            )
        return NursingWriteOutcome(
            encounter_uuid=ref, obs_uuids=_top_obs_uuids(body.get("obs")), created=False
        )

    def _check_write(
        self,
        patient_uuid: str,
        visit_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        recorded_at: datetime,
        client_ref: UUID,
    ) -> str:
        """Every write's shared checks (before any I/O); the marker to send."""
        if not self._config.nursing_encounter_type_uuid:
            raise ConfigError("nursing config: nursing_encounter_type_uuid is required")
        _check_uuid("patient_uuid", patient_uuid)
        _check_uuid("visit_uuid", visit_uuid)
        _check_uuid("provider_uuid", provider_uuid)
        _check_uuid("location_uuid", location_uuid)
        to_epoch_ms(recorded_at)  # naive -> ValueError
        return _marker(client_ref)

    async def _io_set_members(self, set_uuid: str) -> tuple[str, str, str]:
        cached = self._io_members.get(set_uuid)
        if cached is not None:
            return cached
        _check_uuid("intake/output concept set", set_uuid)
        resp = await self._http.request("GET", f"{_CONCEPT}/{set_uuid}", params={"v": _IO_SET_REP})
        body = resp.json() if resp.status_code == 200 and resp.content else None
        members = body.get("setMembers") if isinstance(body, dict) else None
        by_type: dict[str, list[str]] = {}
        for m in members if isinstance(members, list) else []:
            if not isinstance(m, dict) or not isinstance(m.get("uuid"), str):
                continue
            dt = m.get("datatype")
            name = _text(dt.get("display")) if isinstance(dt, dict) else ""
            by_type.setdefault(name, []).append(m["uuid"])
        picked = [by_type.get(t, []) for t in ("Text", "Numeric", "Datetime")]
        if any(len(uuids) != 1 for uuids in picked):
            raise ConfigError(
                f"io_concepts_incomplete: concept set {set_uuid} needs exactly one Text "
                "(route), one Numeric (quantity) and one Datetime member"
            )
        found = (picked[0][0], picked[1][0], picked[2][0])
        self._io_members[set_uuid] = found
        return found

    async def _write(
        self,
        patient_uuid: str,
        visit_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        recorded_at: datetime,
        client_ref: UUID,
        observations: list[dict[str, Any]],
    ) -> NursingWriteOutcome:
        """One encounter POST named ``str(client_ref)``, unless that encounter exists
        already; the marker on the first obs."""
        marker = self._check_write(
            patient_uuid, visit_uuid, provider_uuid, location_uuid, recorded_at, client_ref
        )
        earlier = await self.find_write_by_ref(visit_uuid, client_ref)
        if earlier is not None:
            return earlier
        encounter_type = self._config.nursing_encounter_type_uuid
        observations[0]["comment"] = marker
        sent = str(client_ref)
        body = {
            "encounterUuid": sent,
            "patientUuid": patient_uuid,
            "visitUuid": visit_uuid,
            "locationUuid": location_uuid,
            "encounterTypeUuid": encounter_type,
            "encounterDateTime": to_epoch_ms(recorded_at),
            "providers": [{"uuid": provider_uuid}],
            "observations": observations,
            "orders": [],
            "drugOrders": [],
        }
        resp = await self._http.request("POST", _ENCOUNTER, json=body)
        if resp.status_code == 404:
            raise NotFound("bahmni: bahmniencounter route not found (HTTP 404)")
        parsed = resp.json() if resp.content else None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("encounterUuid"), str):
            raise VendorRejected("bahmni: encounter response had no encounterUuid", landed=True)
        if parsed["encounterUuid"] != sent:
            raise VendorRejected(
                "bahmni: encounter response named another encounterUuid than the one sent",
                landed=True,
            )
        return NursingWriteOutcome(
            encounter_uuid=sent, obs_uuids=_top_obs_uuids(parsed.get("observations")), created=True
        )

    # ─── Medication chart ────────────────────────────────────────────────────

    async def medication_schedule(
        self,
        patient_uuid: str,
        start: datetime,
        end: datetime,
        *,
        visit_uuid: str | None = None,
    ) -> list[MarSlot]:
        """The patient's chart slots due in ``[start, end)``, ordered by due time.

        Without ``visit_uuid`` the module reads the patient's active visit.
        """
        _check_uuid("patient_uuid", patient_uuid)
        params = {"patientUuid": patient_uuid}
        params["startTime"] = str(_slot_time(start))
        params["endTime"] = str(_slot_time(end))
        if not start < end:
            raise ValueError("bahmni: medication schedule end must be after start")
        if visit_uuid is not None:
            _check_uuid("visit_uuid", visit_uuid)
            params["visitUuid"] = visit_uuid
        body = await self._schedule_get(params)
        slots: list[MarSlot] = []
        for schedule in body:
            if not isinstance(schedule, dict) or "slots" not in schedule:
                raise VendorRejected("bahmni: medication schedule entry carried no slots")
            raw_slots = schedule["slots"] if schedule["slots"] is not None else []
            if not isinstance(raw_slots, list):
                raise VendorRejected("bahmni: medication schedule slots was not a list")
            slots.extend(_mar_slot(r) for r in raw_slots)
        return sorted(slots, key=lambda s: (s.start_at, s.slot_uuid))

    async def create_medication_schedule(self, w: MarScheduleWrite) -> MarScheduleOutcome:
        """Put an active drug order on the chart, once.

        The order's slots are read first: when it is on the chart already nothing is
        sent and its slots are returned (``created=False``). Otherwise one POST, then
        one read of the order's slots for the outcome. A scheduled order needs at
        least one slot; an as-needed order is charted with none.
        """
        if not isinstance(w, MarScheduleWrite):
            raise ValueError("bahmni: w must be a MarScheduleWrite")
        for name in ("patient_uuid", "order_uuid", "provider_uuid"):
            _check_uuid(name, getattr(w, name))
        wire_type = _SCHEDULE_SERVICE_TYPE.get(w.service_type)
        if wire_type is None:
            raise ValueError(f"bahmni: unsupported medication service type: {w.service_type!r}")
        arrays = {
            "firstDaySlotsStartTime": [_slot_time(t) for t in w.first_day_slots],
            "dayWiseSlotsStartTime": [_slot_time(t) for t in w.day_wise_slots],
            "remainingDaySlotsStartTime": [_slot_time(t) for t in w.remaining_day_slots],
        }
        every = [t for times in arrays.values() for t in times]
        if w.service_type == "MedicationRequest" and not every:
            raise ValueError("bahmni: a scheduled medication needs at least one slot time")
        if w.service_type != "MedicationRequest" and every:
            raise ValueError("bahmni: an as-needed medication is charted without slot times")
        if w.comments is not None and (
            not isinstance(w.comments, str) or len(w.comments) > _MAX_SCHEDULE_COMMENT_CHARS
        ):
            raise ValueError(
                f"bahmni: schedule comments must be at most {_MAX_SCHEDULE_COMMENT_CHARS} chars"
            )
        existing = await self._order_slots(w.patient_uuid, w.order_uuid, wire_type)
        if existing:
            return MarScheduleOutcome(
                order_uuid=w.order_uuid,
                created=False,
                slot_uuids=tuple(s.slot_uuid for s in existing),
            )
        body = {
            "patientUuid": w.patient_uuid,
            "orderUuid": w.order_uuid,
            "providerUuid": w.provider_uuid,
            "comments": w.comments,
            "slotStartTime": min(every) if every else None,
            **arrays,
            # Slot times are given explicitly; the module repeats the day-wise ones daily.
            "medicationFrequency": "FIXED_SCHEDULE_FREQUENCY",
            "serviceType": wire_type,
        }
        resp = await self._http.request("POST", _MEDICATION_SCHEDULE, json=body)
        if resp.status_code == 404:
            raise NotFound("bahmni: medication schedule route not found (HTTP 404)")
        created = await self._order_slots(w.patient_uuid, w.order_uuid, wire_type)
        if not created:
            # The create was accepted but charted nothing: a scheduled order gets
            # ceil(quantity / dose) slots, so a zero or sub-dose quantity charts none.
            raise VendorRejected(
                f"bahmni: medication schedule for order {w.order_uuid} charted no slots "
                "(check the order's quantity and dose)"
            )
        return MarScheduleOutcome(
            order_uuid=w.order_uuid,
            created=True,
            slot_uuids=tuple(s.slot_uuid for s in created),
        )

    async def record_administration(self, w: MarAdministrationWrite) -> MarAdministrationOutcome:
        """Record a scheduled dose (given, not given or stopped) on its chart slot.

        The order's slots are read first. A slot that already holds an administration
        is answered with it (``created=False``; ``ref_matched`` says whether it is this
        write's own earlier one) and nothing is sent. Otherwise an order the read shows
        stopped is ``VendorRejected`` with code ``order_not_active``, and a slot that is
        not on the order or no longer open is ``slot_state_invalid``. After the POST
        the slot is read back: an administration without this write's note was
        recorded by someone else in between (``created=False``).
        """
        marker = _check_administration(w)
        if w.slot_uuid is None:
            raise ValueError("bahmni: a scheduled administration needs its slot_uuid")
        _check_uuid("slot_uuid", w.slot_uuid)
        slot_uuid = w.slot_uuid
        rows = await self._order_slot_rows(w.patient_uuid, w.order_uuid, "MEDICATION_REQUEST")
        target = next(((s, r) for s, r in rows if s.slot_uuid == slot_uuid), None)
        if target is None:
            raise _refusal(
                "slot_state_invalid",
                f"slot {slot_uuid} is not a scheduled slot of order {w.order_uuid}",
            )
        slot, raw = target
        admin = raw.get("medicationAdministration")
        if isinstance(admin, dict):
            return _outcome(slot_uuid, admin, marker, created=False)
        if any(_order_stopped(r) for _, r in rows):
            raise _refusal("order_not_active", f"order {w.order_uuid} is stopped")
        if slot.status not in _RECORDABLE_SLOT_STATUSES:
            raise _refusal(
                "slot_state_invalid",
                f"slot {slot_uuid} is {slot.status} with no administration",
            )
        body = _administration_body(w, marker)
        body["slotUuid"] = slot_uuid
        resp = await self._http.request("POST", _SCHEDULED_ADMINISTRATIONS, json=[body])
        if resp.status_code == 404:
            raise NotFound("bahmni: scheduled administration route not found (HTTP 404)")
        parsed = resp.json() if resp.content else None
        answered = (
            parsed[0].get("uuid")
            if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict)
            else None
        )
        if not isinstance(answered, str) or not answered:
            raise VendorRejected(
                "bahmni: scheduled administration answer named no administration", landed=True
            )
        after = await self._order_slot_rows(w.patient_uuid, w.order_uuid, "MEDICATION_REQUEST")
        held = next(
            (r.get("medicationAdministration") for s, r in after if s.slot_uuid == slot_uuid), None
        )
        if not isinstance(held, dict) or held.get("uuid") != answered:
            raise VendorRejected(
                f"bahmni: slot {slot_uuid} does not hold the administration {answered} "
                "answered for it",
                landed=True,
            )
        outcome = _outcome(slot_uuid, held, marker, created=False)
        # Our note on it means this write saved it; without it the slot was filled by
        # someone else between the read and the POST and the module answered theirs.
        return replace(outcome, created=outcome.ref_matched)

    async def record_adhoc_administration(
        self, w: MarAdministrationWrite
    ) -> MarAdministrationOutcome:
        """Record an as-needed dose of an active order (``slot_uuid`` None, ``completed``).

        The order's earlier as-needed doses are read first; one carrying this write's
        note is answered (``created=False``, ``ref_matched=True``) and nothing is sent.
        An order that the dose slots or its chart placeholder show stopped is
        ``order_not_active``. After the POST the new dose slot is read back.
        """
        marker = _check_administration(w)
        if w.slot_uuid is not None:
            raise ValueError("bahmni: an as-needed administration names no slot")
        if w.status != "completed":
            raise ValueError("bahmni: an as-needed administration is recorded as completed")
        doses = await self._order_slot_rows(
            w.patient_uuid, w.order_uuid, "AS_NEEDED_MEDICATION_REQUEST"
        )
        for slot, raw in doses:
            admin = raw.get("medicationAdministration")
            if isinstance(admin, dict) and marker in _admin_notes(admin):
                return _outcome(slot.slot_uuid, admin, marker, created=False)
        placeholder = await self._order_slot_rows(
            w.patient_uuid, w.order_uuid, "AS_NEEDED_PLACEHOLDER"
        )
        if any(_order_stopped(r) for _, r in [*doses, *placeholder]):
            raise _refusal("order_not_active", f"order {w.order_uuid} is stopped")
        resp = await self._http.request(
            "POST", _ADHOC_ADMINISTRATIONS, json=_administration_body(w, marker)
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: adhoc administration route not found (HTTP 404)")
        parsed = resp.json() if resp.content else None
        answered = parsed.get("uuid") if isinstance(parsed, dict) else None
        if not isinstance(answered, str) or not answered:
            raise VendorRejected(
                "bahmni: adhoc administration answer named no administration", landed=True
            )
        after = await self._order_slot_rows(
            w.patient_uuid, w.order_uuid, "AS_NEEDED_MEDICATION_REQUEST"
        )
        for slot, raw in after:
            admin = raw.get("medicationAdministration")
            if isinstance(admin, dict) and admin.get("uuid") == answered:
                return _outcome(slot.slot_uuid, admin, marker, created=True)
        raise VendorRejected(
            f"bahmni: no as-needed dose slot of order {w.order_uuid} holds the administration "
            f"{answered}",
            landed=True,
        )

    async def _order_slots(
        self, patient_uuid: str, order_uuid: str, wire_type: str
    ) -> list[MarSlot]:
        rows = await self._order_slot_rows(patient_uuid, order_uuid, wire_type)
        return sorted((slot for slot, _ in rows), key=lambda s: (s.start_at, s.slot_uuid))

    async def _order_slot_rows(
        self, patient_uuid: str, order_uuid: str, wire_type: str
    ) -> list[tuple[MarSlot, dict[str, Any]]]:
        """The order's slots of one service type, each with its raw answer."""
        body = await self._schedule_get(
            {"patientUuid": patient_uuid, "orderUuids": order_uuid, "serviceType": wire_type}
        )
        slots = [_mar_slot(r) for r in body]
        if any(s.order_uuid != order_uuid for s in slots):
            raise VendorRejected(
                f"bahmni: medication slot read for order {order_uuid} named another order"
            )
        # The module filters by service type (scheduled only when none is named); a
        # slot of another type means the filter was not applied, so an empty answer
        # for this type would prove nothing either.
        expected = _SERVICE_TYPE_ANSWER[wire_type]
        if any(s.service_type != expected for s in slots):
            raise VendorRejected(
                f"bahmni: medication slot read for order {order_uuid} answered a service "
                f"type other than {expected}"
            )
        return list(zip(slots, cast(list[dict[str, Any]], body), strict=True))

    async def _schedule_get(self, params: dict[str, str]) -> list[Any]:
        resp = await self._http.request("GET", _MEDICATION_SCHEDULE, params=params)
        if resp.status_code == 404:
            raise NotFound("bahmni: medication schedule route not found (HTTP 404)")
        body = resp.json() if resp.content else None
        if not isinstance(body, list):
            raise VendorRejected("bahmni: medication schedule response was not a list")
        return body
