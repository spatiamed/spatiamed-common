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
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.ipd_care_types import BedStatus, WardBed, WardLayout, WardPatient

_REST = "/openmrs/ws/rest/v1"
_WARDS = f"{_REST}/admissionLocation"
_LOCATION = f"{_REST}/location"
_CONCEPT = f"{_REST}/concept"
_IPD_WARDS = f"{_REST}/ipd/wards"
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


class BahmniIpdCareClient:
    def __init__(self, http: BahmniHttp, config: NursingConfig) -> None:
        self._http = http
        self._config = config

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
