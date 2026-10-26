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
    and provider. The first obs carries the comment ``"sm:" + str(client_ref)``, which
    ``find_write_by_ref`` looks for. A write does not look for an earlier one itself:
    a caller retrying after an unknown outcome calls ``find_write_by_ref`` first, since
    re-sending the same encounter uuid would add the obs again. Values are checked
    against data-entry limits before any I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sm_common.integrations.adapters.bahmni_http import BahmniHttp, to_epoch_ms
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.ipd_care_types import (
    BedStatus,
    IntakeOutputWrite,
    NursingWriteOutcome,
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
_MARKER_PREFIX = "sm:"
_MARKER_REP = "custom:(encounters:(uuid,obs:(uuid,comment)))"
_IO_SET_REP = "custom:(setMembers:(uuid,display,datatype:(display)))"
_MAX_NOTE_CHARS = 4000
_MAX_IO_ROUTE_CHARS = 255
_MAX_IO_QUANTITY_ML = 10000
_CONCEPT_DATATYPE_REP = "custom:(uuid,datatype:(display))"
# OpenMRS uuids are up to 38 chars of [A-Za-z0-9-]; anything else could reach another route.
_UUID_SHAPE = re.compile(r"[A-Za-z0-9-]{1,38}")
_MAX_WARD_PATIENTS_PAGE = 200

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
        """The earlier write carrying ``client_ref``'s marker in this visit, else ``None``.

        The outcome names every top-level obs of the encounter that carries the marker.
        """
        _check_uuid("visit_uuid", visit_uuid)
        marker = _marker(client_ref)
        resp = await self._http.request("GET", f"{_VISIT}/{visit_uuid}", params={"v": _MARKER_REP})
        if resp.status_code == 404:
            raise NotFound(f"bahmni: visit {visit_uuid} not found (HTTP 404)")
        body = resp.json() if resp.content else None
        rows = body.get("encounters") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise VendorRejected("bahmni: visit response carried no encounter list")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("uuid"), str):
                continue
            obs = row.get("obs")
            if not isinstance(obs, list):
                continue
            if any(isinstance(o, dict) and o.get("comment") == marker for o in obs):
                return NursingWriteOutcome(
                    encounter_uuid=row["uuid"], obs_uuids=_top_obs_uuids(obs), created=False
                )
        return None

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
        """One encounter POST named ``str(client_ref)``; the marker on the first obs."""
        marker = self._check_write(
            patient_uuid, visit_uuid, provider_uuid, location_uuid, recorded_at, client_ref
        )
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
