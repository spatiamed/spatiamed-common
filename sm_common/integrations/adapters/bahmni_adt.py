"""Bahmni inpatient (ADT) surface: wards, beds, and bed assignments.

Mixed into ``BahmniRecordClient`` so the record client module does not grow
further. Every call goes through the shared ``BahmniHttp`` (auth, retry-on-401,
error mapping). Response shapes follow Bahmni's bed-management module:

- ``GET admissionLocation`` lists wards with bed counts (no layout).
- ``GET admissionLocation/<uuid>?v=full`` adds ``bedLayouts``: one entry per
  layout cell, and a cell with no bed mapped carries ``bedId: null``.
- ``GET beds?patientUuid=`` returns the patient's current bed with only its id,
  number and ward: Bahmni returns no bed uuid and a null bed type there, so the
  assignment is completed from the ward layout.

Worklists are Bahmni's saved SQL queries (``bahmnicore/sql``), answered as a bare
JSON array of rows keyed by column label, with SQL NULL as ``""``. Dispositions
(admit advice, discharge order) ride a consultation encounter.

Admit and transfer are two Bahmni calls that are not atomic: an ADMISSION/TRANSFER
encounter, then ``POST beds/<bedId>``. Bahmni does not lock a bed to one patient.
Both methods are therefore re-runnable with the same ``event_id``: the encounter is
found by its marker obs, the bed is re-read before any write, and an assignment
already in place is left alone. Two clients can both see a bed AVAILABLE between
that re-read and ``POST beds``; callers serialise assignments per bed.

A bed's id is its only durable key. bedmanagement 6.0.0 frees a bed (transfer out,
discharge, ``DELETE beds/<id>``) by saving a ``Bed`` built from a projection of only
its id and number (``BedManagementDaoImpl.getBedByPatient``); that fresh object carries
a random uuid and no bed type, and the full-row update writes both, so the bed's uuid
changes and its bed type is cleared every time a patient leaves it. Beds are
therefore read by id (``beds/<id>`` + the ward layout), never by ``bed/<uuid>``, and a
returned ``bed_uuid`` is only the uuid as of that read.

OpenMRS privileges: besides the bed-management and encounter privileges, an admission
on an install with bahmni-ipd (1.1.x) needs ``Add Tasks`` and ``Edit Tasks``: the IPD
module creates nursing tasks when the ADMISSION is saved, and without them the write
is refused (HTTP 403, ``AuthError``).

Discharge is ``POST bahmnicore/discharge`` (Bahmni frees the patient's bed and files
the DISCHARGE encounter in one transaction), then ``bahmnicore/visit/endVisit``: the
discharge alone leaves the visit open. Both steps are re-runnable on ``event_id``.

Ward, bed type and bed creation (master data) are re-runnable by lookup: the ward by
name under its parent, the bed type by name, the bed by its layout cell.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp, to_epoch_ms
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    ConfigError,
    ConflictError,
    EncounterMatchAmbiguous,
    EncounterOutsideVisit,
    HmsAdapterError,
    NotFound,
    TransientError,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.record_types import (
    AdtList,
    RecordAdmitResult,
    RecordAdtInput,
    RecordAdtRow,
    RecordBed,
    RecordBedAssignment,
    RecordEncounterResult,
    RecordVisit,
    RecordWard,
)

log = logging.getLogger(__name__)

_REST = "/openmrs/ws/rest/v1"
_WARDS = f"{_REST}/admissionLocation"
_BEDS = f"{_REST}/beds"
_LOCATION = f"{_REST}/location"
_SQL = f"{_REST}/bahmnicore/sql"
_ENCOUNTER = f"{_REST}/bahmnicore/bahmniencounter"
_END_AND_CREATE = f"{_REST}/bahmnicore/visit/endVisitAndCreateEncounter"
_END_VISIT = f"{_REST}/bahmnicore/visit/endVisit"
_DISCHARGE = f"{_REST}/bahmnicore/discharge"
# ADT routes whose 404 is a deployment fault (NotFound); bahmniencounter's is WriteNotSupported.
_NOT_FOUND_ROUTES = {_END_AND_CREATE: "endVisitAndCreateEncounter", _DISCHARGE: "discharge"}
_BED = f"{_REST}/bed"
_BEDTYPE = f"{_REST}/bedtype"
_LOCATION_TAG = f"{_REST}/locationtag"
# Bed management creates wards only when a location tag of exactly this name exists.
_ADMISSION_TAG = "Admission Location"
# Column widths in Bahmni's schema (location.name, bed_type.name/display_name, bed.bed_number).
_MAX_LOCATION_NAME = 255
_MAX_BED_TYPE_NAME = 255
_MAX_BED_TYPE_DISPLAY = 10
_MAX_BED_NUMBER = 10
# Bahmni sets a layout one SELECT (+ INSERT) per cell in one transaction; 50 x 50 = 2,500
# cells is far past any real ward grid and keeps a typo from pinning the server.
_MAX_LAYOUT_SIDE = 50
_WARD_SEARCH_REP = "custom:(uuid,name,retired,parentLocation:(uuid),tags:(name))"
_WORKLIST_QUERY: dict[str, str] = {
    "to_admit": "emrapi.sqlSearch.patientsToAdmit",
    "admitted": "emrapi.sqlSearch.admittedPatients",
    "to_discharge": "emrapi.sqlSearch.patientsToDischarge",
}
# Optional worklist columns, matched case-insensitively. Bahmni's stock queries
# return none of them (only name, identifier, uuid, activeVisitUuid, hasBeenAdmitted);
# an install that customises its queries can add them under these labels.
_WARD_COLUMNS = frozenset({"ward", "wardname", "ward name"})
_BED_COLUMNS = frozenset({"bed", "bednumber", "bed number"})
_ADVISED_COLUMNS = frozenset({"advisedat", "advised_at", "advised on"})
_CORE_COLUMNS = frozenset({"uuid", "identifier", "name", "activevisituuid"})
_MARKER_REP = "custom:(uuid,obs:(concept:(uuid),value))"
# One visit read for ADT: open?, whose, which type, and each encounter's type + markers.
# OpenMRS's custom-rep parser breaks when anything follows "))", so the nested group is last.
_ADT_VISIT_REP = (
    "custom:(uuid,stopDatetime,patient:(uuid),visitType:(uuid),"
    "encounters:(uuid,voided,encounterType:(uuid),obs:(concept:(uuid),value)))"
)
_ADT_MARKER = "sm:adt:"
# Read after a bare HTTP 500 from bahmniencounter, to tell its terminal causes apart.
_ENCOUNTER_500_REP = (
    "custom:(uuid,startDatetime,stopDatetime,"
    "encounters:(uuid,voided,encounterType:(uuid),encounterProviders:(provider:(uuid))))"
)
# OpenMRS uuids are up to 38 chars of [A-Za-z0-9-]; anything else could reach another route.
_UUID_SHAPE = re.compile(r"[A-Za-z0-9-]{1,38}")
# Bahmni pages admissionLocation (50 rows by default); more pages than this is not a ward list.
_MAX_WARD_PAGES = 20


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _label(v: Any) -> str:
    if not isinstance(v, dict):
        return ""
    return _text(v.get("display")) or _text(v.get("name"))


def _ref_uuid(v: Any) -> str | None:
    uuid = v.get("uuid") if isinstance(v, dict) else None
    return uuid if isinstance(uuid, str) else None


def _outside_visit(visit: dict[str, Any], at: datetime) -> bool:
    """True when ``at`` is before the visit's start or after its stop (bounds included)."""
    at_ms = to_epoch_ms(at)
    start = when(visit.get("startDatetime"))
    stop = when(visit.get("stopDatetime"))
    if start is not None and at_ms < to_epoch_ms(start):
        return True
    return stop is not None and at_ms > to_epoch_ms(stop)


def _sent_uuid(sent: str, parsed: Any) -> str:
    """The response's encounterUuid, which must be the one the write named."""
    uuid = parsed.get("encounterUuid") if isinstance(parsed, dict) else None
    if not isinstance(uuid, str) or not uuid:
        raise VendorRejected("bahmni: encounter response had no encounterUuid", landed=True)
    if uuid != sent:
        raise VendorRejected(
            "bahmni: encounter response named another encounterUuid than the one sent",
            landed=True,
        )
    return uuid


def _check_uuid(name: str, value: str) -> None:
    """Caller input check (ValueError, as for other bad arguments) before it enters a path."""
    if not isinstance(value, str) or not _UUID_SHAPE.fullmatch(value):
        raise ValueError(f"bahmni: {name} is not a uuid: {value!r}")


def _check_text(name: str, value: str, max_len: int) -> str:
    """A required master-data label: stripped, non-empty, within Bahmni's column width."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > max_len:
        raise ValueError(f"bahmni: {name} must be 1-{max_len} characters: {value!r}")
    return text


def _check_position(name: str, value: int, max_value: int | None = None) -> None:
    if _int(value) is None or value < 1 or (max_value is not None and value > max_value):
        limit = f"1-{max_value}" if max_value is not None else "a positive integer"
        raise ValueError(f"bahmni: {name} must be {limit}: {value!r}")


def _same(a: str | None, b: str) -> bool:
    """Bahmni's name and uuid lookups follow the database collation (case-insensitive)."""
    return a is not None and a.strip().casefold() == b.strip().casefold()


def _has_next(body: dict[str, Any]) -> bool:
    links = body.get("links")
    return isinstance(links, list) and any(
        isinstance(link, dict) and link.get("rel") == "next" for link in links
    )


def _results(resp_body: Any, what: str) -> list[Any]:
    rows = resp_body.get("results") if isinstance(resp_body, dict) else None
    if not isinstance(rows, list):
        raise VendorRejected(f"bahmni: {what} response carried no results list")
    return rows


def _ward(raw: Any, beds: tuple[RecordBed, ...]) -> RecordWard:
    ward = raw.get("ward") if isinstance(raw, dict) else None
    uuid = ward.get("uuid") if isinstance(ward, dict) else None
    total = _int(raw.get("totalBeds")) if isinstance(raw, dict) else None
    occupied = _int(raw.get("occupiedBeds")) if isinstance(raw, dict) else None
    if not isinstance(uuid, str) or not uuid or total is None or occupied is None:
        raise VendorRejected("bahmni: admission location missing ward uuid or bed counts")
    return RecordWard(
        uuid=uuid, name=_label(ward), total_beds=total, occupied_beds=occupied, beds=beds
    )


def _bed(raw: Any) -> RecordBed | None:
    """One layout cell -> a bed; ``None`` for a cell with no bed mapped."""
    if not isinstance(raw, dict):
        raise VendorRejected("bahmni: bed layout entry was not an object")
    if raw.get("bedId") is None:
        return None
    bed_id = _int(raw.get("bedId"))
    bed_uuid = _text(raw.get("bedUuid"))
    row, column = _int(raw.get("rowNumber")), _int(raw.get("columnNumber"))
    if bed_id is None or not bed_uuid or row is None or column is None:
        raise VendorRejected("bahmni: bed layout entry missing bedId, bedUuid or position")
    bed_type = raw.get("bedType")
    patients = raw.get("patients") or []
    if not isinstance(patients, list):
        raise VendorRejected("bahmni: bed layout patients was not a list")
    return RecordBed(
        bed_id=bed_id,
        bed_uuid=bed_uuid,
        bed_number=_text(raw.get("bedNumber")),
        # The type name is what bed create takes; displayName is presentation only.
        bed_type=_text(bed_type.get("name")) if isinstance(bed_type, dict) else "",
        status=_text(raw.get("status")),
        row=row,
        column=column,
        patient_uuids=tuple(
            p["uuid"] for p in patients if isinstance(p, dict) and isinstance(p.get("uuid"), str)
        ),
    )


def _optional(v: Any) -> str | None:
    text = "" if v is None else str(v).strip()
    return text or None


def _worklist_row(raw: Any) -> RecordAdtRow:
    if not isinstance(raw, dict):
        raise VendorRejected("bahmni: worklist row was not an object")
    patient, visit = raw.get("uuid"), raw.get("activeVisitUuid")
    if not isinstance(patient, str) or not patient or not isinstance(visit, str) or not visit:
        raise VendorRejected("bahmni: worklist row missing uuid or activeVisitUuid")
    ward = bed = advised = None
    extra: dict[str, str] = {}
    for key, value in raw.items():
        k = str(key).lower()
        if k in _CORE_COLUMNS:
            continue
        if k in _WARD_COLUMNS:
            ward = _optional(value)
        elif k in _BED_COLUMNS:
            bed = _optional(value)
        elif k in _ADVISED_COLUMNS:
            advised = when(value) if value != "" else None
        else:
            extra[str(key)] = "" if value is None else str(value)
    return RecordAdtRow(
        patient_uuid=patient,
        identifier=_text(raw.get("identifier")),
        name=_text(raw.get("name")),
        visit_uuid=visit,
        ward=ward,
        bed_number=bed,
        advised_at=advised,
        extra=extra,
    )


class BahmniAdtMixin:
    _http: BahmniHttp
    _config: BahmniConfig

    if TYPE_CHECKING:
        # Provided by BahmniRecordClient (registration marker lookup).
        async def _encounters(self, visit_uuid: str, rep: str) -> list[Any]: ...

        def _has_marker(self, row: Any, marker: str) -> str | None: ...

        async def active_visit(self, patient_uuid: str) -> RecordVisit | None: ...

        async def start_visit(
            self, patient_uuid: str, visit_type_uuid: str | None = None
        ) -> tuple[RecordVisit, bool]: ...

        async def list_visits(self, patient_uuid: str, limit: int = 20) -> list[RecordVisit]: ...

    # ─── Encounter writes (shared) ───────────────────────────────────────────

    async def _visit_and_encounters(
        self, visit_uuid: str, rep: str
    ) -> tuple[dict[str, Any], list[Any]]:
        """One visit with its start/stop and its encounters in ``rep`` (one GET)."""
        inner = rep.removeprefix("custom:(").removesuffix(")")
        resp = await self._http.request(
            "GET",
            f"{_REST}/visit/{visit_uuid}",
            params={"v": f"custom:(uuid,startDatetime,stopDatetime,encounters:({inner}))"},
        )
        if resp.status_code == 404:
            raise NotFound(f"bahmni: encounters for visit {visit_uuid} not found (HTTP 404)")
        body = resp.json() if resp.content else None
        rows = body.get("encounters") if isinstance(body, dict) else None
        if not isinstance(body, dict) or not isinstance(rows, list):
            raise VendorRejected("bahmni: visit response carried no encounter list")
        return body, rows

    @staticmethod
    def _check_in_visit(visit: dict[str, Any], at: datetime) -> None:
        """Refuse, before writing, an encounter time Bahmni would answer with a bare 500."""
        if _outside_visit(visit, at):
            raise EncounterOutsideVisit(
                "bahmni: encounter time is outside the visit's start/stop range"
            )

    async def _diagnose_encounter_500(
        self, body: dict[str, Any], at: datetime | None
    ) -> HmsAdapterError | None:
        """The terminal cause of a bare HTTP 500 from ``bahmniencounter``, if one shows.

        Bahmni answers both causes with the same HTML 500 page, so the visit is read
        again: an encounter time outside it (it may have been closed meanwhile), or,
        for a write naming no ``encounterUuid``, two or more unvoided encounters of the
        same type by the same provider (Bahmni then cannot pick one to merge into). Any
        other 500 stays transient. The provider is matched, not the session window, so
        this can call ambiguous a 500 that had another cause.
        """
        visit_uuid = body.get("visitUuid")
        if not isinstance(visit_uuid, str) or not _UUID_SHAPE.fullmatch(visit_uuid):
            return None
        try:
            resp = await self._http.request(
                "GET", f"{_REST}/visit/{visit_uuid}", params={"v": _ENCOUNTER_500_REP}
            )
            visit = resp.json() if resp.status_code == 200 and resp.content else None
        except (HmsAdapterError, ValueError):
            return None
        if not isinstance(visit, dict):
            return None
        if at is not None and _outside_visit(visit, at):
            return EncounterOutsideVisit(
                "bahmni: encounter time is outside the visit's start/stop range"
            )
        if "encounterUuid" in body:
            return None
        providers = {
            p.get("uuid") for p in body.get("providers") or [] if isinstance(p, dict)
        }
        matching = 0
        for row in visit.get("encounters") or []:
            if not isinstance(row, dict) or row.get("voided") is True:
                continue
            if _ref_uuid(row.get("encounterType")) != body.get("encounterTypeUuid"):
                continue
            by = {
                _ref_uuid(ep.get("provider"))
                for ep in row.get("encounterProviders") or []
                if isinstance(ep, dict)
            }
            if by & providers:
                matching += 1
        if matching >= 2:
            return EncounterMatchAmbiguous(
                "bahmni: more than one encounter matches; the write must name its encounterUuid"
            )
        return None

    async def _post_encounter(
        self, url: str, body: dict[str, Any], params: dict[str, str] | None, at: datetime | None
    ) -> Any:
        """POST an encounter write; a bare 500 with a terminal cause raises that cause."""
        try:
            return await self._http.request("POST", url, params=params, json=body)
        except TransientError as exc:
            if exc.status_code != 500 or url != _ENCOUNTER:
                raise
            terminal = await self._diagnose_encounter_500(body, at)
            if terminal is None:
                raise
            raise terminal from exc

    # ─── Wards and beds (read) ───────────────────────────────────────────────

    async def list_wards(self) -> list[RecordWard]:
        """Every admission location with Bahmni's bed counts; ``beds`` is empty.

        A 404 means the bed-management module is absent and raises ``NotFound``
        (never "no wards"); ``get_ward`` answers ``None`` for one missing ward.
        """
        out: list[RecordWard] = []
        params: dict[str, str] | None = None
        for _ in range(_MAX_WARD_PAGES):
            resp = await self._http.request("GET", _WARDS, params=params)
            if resp.status_code == 404:
                raise NotFound("bahmni: admissionLocation route not found (HTTP 404)")
            body = resp.json() if resp.content else None
            out += [_ward(r, ()) for r in _results(body, "admission location")]
            if not _has_next(body):
                return out
            params = {"startIndex": str(len(out))}
        raise VendorRejected(f"bahmni: admission locations exceed {_MAX_WARD_PAGES} pages")

    async def get_ward(self, ward_uuid: str) -> RecordWard | None:
        """One ward with its beds (layout cells without a bed are skipped).

        ``None`` means no such location. Bahmni answers 500 (not 404) for an unknown
        ward uuid, so a ``TransientError`` is checked against ``GET location/<uuid>``:
        404 there -> ``None``; anything else re-raises the original error. A 404 on
        ``admissionLocation`` itself is a missing route -> ``NotFound``. A location
        that is not a ward answers with zero beds.
        """
        found = await self._ward_layout(ward_uuid)
        return found[0] if found else None

    async def _ward_layout(self, ward_uuid: str) -> tuple[RecordWard, set[tuple[int, int]]] | None:
        """``get_ward`` plus every layout cell position, empty cells included."""
        _check_uuid("ward_uuid", ward_uuid)
        try:
            resp = await self._http.request("GET", f"{_WARDS}/{ward_uuid}", params={"v": "full"})
        except TransientError as original:
            if await self._location_missing(ward_uuid):
                return None
            raise original
        if resp.status_code == 404:
            raise NotFound("bahmni: admissionLocation route not found (HTTP 404)")
        body = resp.json() if resp.content else None
        layouts = body.get("bedLayouts") if isinstance(body, dict) else None
        if not isinstance(layouts, list):
            raise VendorRejected("bahmni: admission location carried no bedLayouts list")
        beds = sorted(
            (b for raw in layouts if (b := _bed(raw)) is not None),
            key=lambda b: (b.row, b.column, b.bed_id),
        )
        cells = {
            (row, col)
            for raw in layouts
            if isinstance(raw, dict)
            and (row := _int(raw.get("rowNumber"))) is not None
            and (col := _int(raw.get("columnNumber"))) is not None
        }
        return _ward(body, tuple(beds)), cells

    async def _location_missing(self, uuid: str) -> bool:
        """True only when Bahmni's core location route says 404 for ``uuid``."""
        try:
            resp = await self._http.request(
                "GET", f"{_LOCATION}/{uuid}", params={"v": "custom:(uuid)"}
            )
        except TransientError:
            return False
        return resp.status_code == 404

    async def current_bed(self, patient_uuid: str) -> RecordBedAssignment | None:
        """The patient's current bed, or ``None`` when Bahmni has none assigned.

        Resolve the patient first: Bahmni answers an unknown patient uuid with an
        empty list, which reads as "unassigned". ``ward_uuid`` is the location the
        bed is mapped to, which may be a child of a ward ``list_wards`` returns, so
        do not match it against those uuids for equality.
        """
        resp = await self._http.request("GET", _BEDS, params={"patientUuid": patient_uuid})
        if resp.status_code == 404:
            # Never "unassigned": an empty answer would unlock a second bed.
            raise NotFound("bahmni: beds route not found (HTTP 404)")
        rows = _results(resp.json() if resp.content else None, "bed assignment")
        if not rows:
            return None
        if len(rows) > 1 or not isinstance(rows[0], dict):
            raise VendorRejected("bahmni: bed assignment response was not a single bed")
        ward, bed = await self._bed_in_layout(rows[0], "bed assignment")
        return RecordBedAssignment(
            bed_id=bed.bed_id,
            bed_uuid=bed.bed_uuid,
            bed_number=bed.bed_number,
            bed_type=bed.bed_type,
            ward_uuid=ward.uuid,
            ward_name=ward.name,
        )

    async def _bed_in_layout(self, details: Any, what: str) -> tuple[RecordWard, RecordBed]:
        """A ``beds`` (BedDetails) row -> its ward and its layout entry, read by bed id.

        The layout entry carries the bed's status, patients and its uuid as of now.
        """
        if not isinstance(details, dict):
            raise VendorRejected(f"bahmni: {what} was not an object")
        bed_id = _int(details.get("bedId"))
        location = details.get("physicalLocation")
        ward_uuid = location.get("uuid") if isinstance(location, dict) else None
        if bed_id is None or not isinstance(ward_uuid, str) or not _UUID_SHAPE.fullmatch(ward_uuid):
            raise VendorRejected(f"bahmni: {what} missing bedId or ward")
        ward = await self.get_ward(ward_uuid)
        bed = next((b for b in ward.beds if b.bed_id == bed_id), None) if ward else None
        if ward is None or bed is None:
            raise VendorRejected(f"bahmni: bed {bed_id} not in its ward layout")
        return ward, bed

    async def _bed_by_id(self, bed_id: int) -> tuple[RecordWard, RecordBed]:
        """Bed ``bed_id`` as its ward layout shows it now; unknown id -> ``NotFound``.

        Read by id, never by uuid: bedmanagement 6.0.0 rewrites a bed's uuid each time a
        patient leaves it (see ``admit``).
        """
        resp = await self._http.request("GET", f"{_BEDS}/{bed_id}")
        if resp.status_code == 404:
            # Unknown bed id or no bed-management route: Bahmni answers 404 for both.
            raise NotFound(f"bahmni: bed {bed_id} not found (HTTP 404)")
        return await self._bed_in_layout(resp.json() if resp.content else None, "bed")

    # ─── Wards, bed types and beds (master data) ─────────────────────────────
    # No BahmniConfig key gates these and no sm:adt: marker applies: each create is
    # made re-runnable by looking up what it would create (ward name, bed type name,
    # bed cell) before writing.

    async def create_ward(
        self, *, name: str, parent_location_uuid: str, rows: int, columns: int
    ) -> RecordWard:
        """A ward (an "Admission Location" location) with a ``rows`` x ``columns`` grid.

        Re-runnable: a ward of this name under ``parent_location_uuid`` is reused. OpenMRS
        keeps location names unique across live locations, so the name is looked up by
        location search (all pages; retired locations are not listed and their names are
        free) before any write:
        - a live "Admission Location" of this name under this parent -> reused;
        - the name held by any other live location (a ward under another parent, or a
          location that is not a ward) -> ``ConflictError("ward_name_taken")``.
        Bahmni's own duplicate check answers the create with 400 ``location.duplicate.name``
        on some installs and a bare HTTP 500 on others, so either answer is followed by the
        same lookup: a match is reused, another holder is ``ward_name_taken``, and a 500
        with no location of that name stays ``TransientError`` (a real server fault). A
        reused ward's layout is read first:
        - a smaller grid that would displace a bed ->
          ``ConflictError("ward_layout_shrink_blocked")``, nothing written (Bahmni would
          answer 500, which never heals);
        - the same grid -> no write;
        - otherwise the layout is set (missing cells added, empty cells beyond dropped).

        - The "Admission Location" tag is checked first: without it Bahmni answers the
          create with HTTP 500, indistinguishable from any other 500 ->
          ``ConfigError`` before any write.
        - The name held by a ward under another parent, or by a location that is not a
          ward -> ``ConflictError("ward_name_taken")``.
        - An unknown parent -> ``NotFound``, checked by ``GET location/<uuid>`` before any
          write (Bahmni's own refusal may be a 400 or a bare 500).
        - Names and uuids compare case-insensitively, as Bahmni's lookups do.
        - A new ward has no beds; a reused ward carries its beds.
        """
        label = _check_text("name", name, _MAX_LOCATION_NAME)
        _check_uuid("parent_location_uuid", parent_location_uuid)
        _check_position("rows", rows, _MAX_LAYOUT_SIDE)
        _check_position("columns", columns, _MAX_LAYOUT_SIDE)
        await self._require_admission_tag()
        if await self._location_missing(parent_location_uuid):
            raise NotFound(f"bahmni: parent location {parent_location_uuid} not found")
        holder = await self._location_holding_name(label, parent_location_uuid)
        if holder is not None:
            return await self._reuse_ward(holder, rows, columns)
        try:
            resp = await self._http.request(
                "POST",
                _WARDS,
                json={"name": label, "parentLocationUuid": parent_location_uuid},
                expect=(200, 201, 400),
            )
        except TransientError as original:
            # The duplicate-name refusal can be a bare 500; so can a create that landed.
            found = await self._location_holding_name(label, parent_location_uuid)
            if found is None:
                raise original
            return await self._reuse_ward(found, rows, columns)
        if resp.status_code == 404:
            raise NotFound("bahmni: admissionLocation route not found (HTTP 404)")
        if resp.status_code == 400:
            if "location.duplicate.name" in resp.text:
                found = await self._location_holding_name(label, parent_location_uuid)
                if found is None:
                    raise ConflictError("ward_name_taken")
                return await self._reuse_ward(found, rows, columns)
            if "Parent location not exist" in resp.text:
                raise NotFound(f"bahmni: parent location {parent_location_uuid} not found")
            raise VendorRejected("bahmni: admission location create refused (HTTP 400)")
        body = resp.json() if resp.content else None
        ward_uuid = _ref_uuid(body.get("ward")) if isinstance(body, dict) else None
        if not ward_uuid:
            raise VendorRejected(
                "bahmni: admission location create returned no ward uuid", landed=True
            )
        return await self._post_layout(ward_uuid, rows, columns)

    async def _reuse_ward(self, ward_uuid: str, rows: int, columns: int) -> RecordWard:
        found = await self._ward_layout(ward_uuid)
        if found is None:
            raise VendorRejected(f"bahmni: ward {ward_uuid} has no location")
        ward, cells = found
        if any(b.row > rows or b.column > columns for b in ward.beds):
            raise ConflictError("ward_layout_shrink_blocked")
        grid = {(r, c) for r in range(1, rows + 1) for c in range(1, columns + 1)}
        if cells == grid:
            return ward
        return replace(await self._post_layout(ward_uuid, rows, columns), beds=ward.beds)

    async def _post_layout(self, ward_uuid: str, rows: int, columns: int) -> RecordWard:
        resp = await self._http.request(
            "POST", f"{_WARDS}/{ward_uuid}", json={"bedLayout": {"row": rows, "column": columns}}
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: admissionLocation route not found (HTTP 404)")
        try:
            ward = _ward(resp.json() if resp.content else None, ())
        except VendorRejected as exc:
            raise VendorRejected(str(exc), landed=True) from exc
        if ward.uuid != ward_uuid:
            raise VendorRejected("bahmni: bed layout response named another ward", landed=True)
        return ward

    async def _require_admission_tag(self) -> None:
        resp = await self._http.request(
            "GET", _LOCATION_TAG, params={"q": _ADMISSION_TAG, "v": "custom:(uuid,name)"}
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: locationtag route not found (HTTP 404)")
        tags = _results(resp.json() if resp.content else None, "location tag")
        if not any(
            isinstance(t, dict) and _same(_text(t.get("name")), _ADMISSION_TAG) for t in tags
        ):
            raise ConfigError(f"bahmni: '{_ADMISSION_TAG}' location tag missing")

    async def _location_holding_name(self, name: str, parent_uuid: str) -> str | None:
        """The ward to reuse for ``name`` under ``parent_uuid``, by location search.

        A live "Admission Location" of that name under ``parent_uuid`` -> its uuid. Any
        other live holder of the name -> ``ConflictError("ward_name_taken")``. ``None``
        when no live location has the name. Names and uuids compare case-insensitively.
        """
        holders = [
            row
            for row in await self._locations_named(name)
            if row.get("retired") is not True and _same(_text(row.get("name")), name)
        ]
        for row in holders:
            tags = row.get("tags") if isinstance(row.get("tags"), list) else []
            tagged = any(
                isinstance(t, dict) and _same(_text(t.get("name")), _ADMISSION_TAG) for t in tags
            )
            uuid = _text(row.get("uuid"))
            if uuid and tagged and _same(_ref_uuid(row.get("parentLocation")), parent_uuid):
                return uuid
        if holders:
            raise ConflictError("ward_name_taken")
        return None

    async def _locations_named(self, name: str) -> list[dict[str, Any]]:
        """Every row of ``GET location?q=<name>`` (a name-prefix match), all pages."""
        out: list[dict[str, Any]] = []
        params = {"q": name, "v": _WARD_SEARCH_REP}
        for _ in range(_MAX_WARD_PAGES):
            resp = await self._http.request("GET", _LOCATION, params=params)
            if resp.status_code == 404:
                raise NotFound("bahmni: location route not found (HTTP 404)")
            body = resp.json() if resp.content else None
            rows = _results(body, "location")
            out += [r for r in rows if isinstance(r, dict)]
            if not _has_next(body):
                return out
            params = {**params, "startIndex": str(len(out))}
        raise VendorRejected(f"bahmni: location search exceeds {_MAX_WARD_PAGES} pages")

    async def ensure_bed_type(self, *, name: str, display_name: str) -> str:
        """The bed type called ``name``, created when absent; returns the name beds use.

        Bahmni does not keep bed type names unique, so the lookup is what keeps a retry
        from adding a second one. The lookup follows Bahmni's collation, so a case
        variant reuses the stored type and its stored name is returned. An existing type
        keeps its own display name. Two
        concurrent first calls can both create; provision bed types from one place.
        """
        label = _check_text("name", name, _MAX_BED_TYPE_NAME)
        display = _check_text("display_name", display_name, _MAX_BED_TYPE_DISPLAY)
        resp = await self._http.request("GET", _BEDTYPE, params={"name": label})
        if resp.status_code == 404:
            raise NotFound("bahmni: bedtype route not found (HTTP 404)")
        for row in _results(resp.json() if resp.content else None, "bed type"):
            # Any row Bahmni's search returns is the type bed create will resolve
            # (same lookup, same collation): reuse it under its stored name.
            stored = _text(row.get("name")) if isinstance(row, dict) else ""
            if stored:
                return stored
        resp = await self._http.request(
            "POST", _BEDTYPE, json={"name": label, "displayName": display}
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: bedtype route not found (HTTP 404)")
        body = resp.json() if resp.content else None
        created = _text(body.get("name")) if isinstance(body, dict) else ""
        if not created:
            raise VendorRejected("bahmni: bed type create returned no name", landed=True)
        return created

    async def add_bed(
        self, *, ward_uuid: str, bed_number: str, bed_type: str, row: int, column: int
    ) -> RecordBed:
        """A bed in an empty cell of the ward's layout; re-runnable.

        Read the ward first, because Bahmni saves the bed BEFORE it maps the cell: its
        own "cell taken" refusal leaves an unmapped bed behind, and a cell outside the
        grid is mapped silently. So, before any write:
        - unknown ward -> ``NotFound``;
        - the cell is outside the layout -> ``ConflictError("bed_cell_outside_layout")``;
        - this bed number already in that cell -> that bed, no write (a retry). The bed
          type is not compared: Bahmni clears a bed's type when a patient leaves it, so a
          used bed reads typeless, and the returned bed carries the type stored now;
        - any other bed in the cell -> ``ConflictError("bed_cell_taken")``;
        - this bed number elsewhere in the ward -> ``ConflictError("bed_number_taken")``.
        A cell filled between the read and the POST is Bahmni's 400, after Bahmni has
        already saved an unmapped bed row -> ``ConflictError("bed_cell_taken_orphaned")``
        plus a warning log, so the orphan can be found and deleted. Numbers and types
        compare case-insensitively, as Bahmni's lookups do. An unknown
        ``bed_type`` is Bahmni's 400 -> ``VendorRejected`` (use ``ensure_bed_type``).
        """
        _check_uuid("ward_uuid", ward_uuid)
        number = _check_text("bed_number", bed_number, _MAX_BED_NUMBER)
        type_name = _check_text("bed_type", bed_type, _MAX_BED_TYPE_NAME)
        _check_position("row", row)
        _check_position("column", column)
        found = await self._ward_layout(ward_uuid)
        if found is None:
            raise NotFound(f"bahmni: ward {ward_uuid} not found")
        ward, cells = found
        if (row, column) not in cells:
            raise ConflictError("bed_cell_outside_layout")
        for bed in ward.beds:
            if (bed.row, bed.column) == (row, column):
                if _same(bed.bed_number, number):
                    # Not the type: Bahmni clears it when a patient leaves the bed.
                    return bed
                raise ConflictError("bed_cell_taken")
        if any(_same(bed.bed_number, number) for bed in ward.beds):
            raise ConflictError("bed_number_taken")
        resp = await self._http.request(
            "POST",
            _BED,
            json={
                "bedNumber": number,
                "bedType": type_name,
                "row": row,
                "column": column,
                "locationUuid": ward_uuid,
            },
            expect=(200, 201, 400),
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: bed route not found (HTTP 404)")
        if resp.status_code == 400:
            if "Already bed assign" in resp.text:
                # Bahmni saved the bed before refusing the cell: an unmapped bed row remains.
                log.warning(
                    "bed_cell_taken_orphaned: ward %s cell (%d,%d) bed number %r; "
                    "Bahmni kept an unmapped bed row",
                    ward_uuid,
                    row,
                    column,
                    number,
                )
                raise ConflictError("bed_cell_taken_orphaned")
            if "Invalid bed type" in resp.text:
                raise VendorRejected(f"bahmni: unknown bed type {type_name!r}")
            raise VendorRejected("bahmni: bed create refused (HTTP 400)")
        body = resp.json() if resp.content else None
        bed_id = _int(body.get("id")) if isinstance(body, dict) else None
        bed_uuid = _text(body.get("uuid")) if isinstance(body, dict) else ""
        if not isinstance(body, dict) or bed_id is None or not bed_uuid:
            raise VendorRejected("bahmni: bed create returned no id or uuid", landed=True)
        returned_type = body.get("bedType")
        return RecordBed(
            bed_id=bed_id,
            bed_uuid=bed_uuid,
            bed_number=_text(body.get("bedNumber")) or number,
            bed_type=_text(returned_type.get("name"))
            if isinstance(returned_type, dict)
            else type_name,
            status=_text(body.get("status")),
            row=_int(body.get("row")) or row,
            column=_int(body.get("column")) or column,
            patient_uuids=(),
        )

    # ─── Worklists ───────────────────────────────────────────────────────────

    async def adt_worklist(self, which: AdtList) -> list[RecordAdtRow]:
        """One of Bahmni's ADT worklists for ``config.visit_location_uuid``.

        Known columns map by name (see ``_*_COLUMNS``); every other column lands in
        ``extra`` as text. Bahmni's stock queries repeat a patient once per identifier
        or person name, so rows are de-duplicated on (patient uuid, visit uuid),
        keeping the first.

        Bahmni resolves ``location_uuid`` up to its nearest "Visit Location" ancestor.
        Three misconfigurations answer 500 (``TransientError``), not an empty list: an
        unset saved query (GP ``emrapi.sqlSearch.*``), an unknown location uuid, and a
        location with no "Visit Location" tag in its ancestry. A location under a
        different visit location answers an empty list; for "admitted" that logs
        ``adt_location_mismatch`` when Bahmni's wards report occupied beds. The ward
        count spans every visit location, so on a multi-branch install the log can
        fire falsely; it is a diagnostic only.

        Bahmni's stock "to admit" query lists a visit only when all of these hold: the
        visit is open and its own location IS the resolved Visit Location (no ancestry:
        a visit at a child location is never listed); one of its encounters carries a
        live obs whose coded answer is named "Admit Patient"; and the visit has no
        ADMISSION encounter yet (the patient also needs an identifier of the
        ``bahmni.primaryIdentifierType``). There is no encounter-type, encounter-location
        or date condition. Visits from ``start_visit`` sit at ``visit_location_uuid``.
        """
        location = self._config.visit_location_uuid
        if not location:
            raise ConfigError("bahmni: visit_location_uuid is required for ADT worklists")
        resp = await self._http.request(
            "GET",
            _SQL,
            params={"q": _WORKLIST_QUERY[which], "location_uuid": location, "v": "full"},
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: bahmnicore/sql route not found (HTTP 404)")
        body = resp.json() if resp.content else None
        if not isinstance(body, list):
            raise VendorRejected("bahmni: worklist response was not a list")
        rows: list[RecordAdtRow] = []
        seen: set[tuple[str, str]] = set()
        for raw in body:
            row = _worklist_row(raw)
            if (row.patient_uuid, row.visit_uuid) not in seen:
                seen.add((row.patient_uuid, row.visit_uuid))
                rows.append(row)
        if not rows and which == "admitted":
            await self._check_location_mismatch(location)
        return rows

    async def _check_location_mismatch(self, location: str) -> None:
        """Diagnostic only: never fails the worklist (auth/config errors still raise)."""
        try:
            occupied = sum(w.occupied_beds for w in await self.list_wards())
        except (NotFound, TransientError, VendorRejected):
            return
        if occupied > 0:
            log.warning(
                "adt_location_mismatch: admitted worklist empty for visit location %s "
                "while wards report %d occupied beds",
                location,
                occupied,
            )

    # ─── Dispositions (admit advice, discharge order) ────────────────────────

    async def write_disposition(
        self,
        *,
        event_id: UUID,
        patient_uuid: str,
        visit_uuid: str,
        provider_uuid: str,
        kind: Literal["admit", "discharge"],
        at: datetime,
        note: str | None,
        encounter_uuid: UUID | None = None,
    ) -> RecordEncounterResult:
        """An ADMIT/DISCHARGE disposition on a consultation encounter; idempotent on
        ``event_id``.

        Bahmni picks the disposition answer from ``code`` (its concept mapping);
        ``conceptName`` (``disposition_codes[kind]``) is sent as Bahmni's own UI does
        but not read by the server. It is the concept name Bahmni's stock worklist
        SQL matches (e.g. "Admit Patient"), so editing it does not change what is
        written. A note needs ``concept_map["disposition_note"]`` (a
        concept uuid): Bahmni resolves disposition notes by uuid only.

        The write names its own encounter: ``encounter_uuid``, else ``event_id``. Bahmni
        honours it, so the disposition is never merged into another encounter, and
        it still lands once two consultation encounters of one provider exist in the
        visit (a write without a uuid then fails). ``at`` outside the visit's start/stop
        range raises ``EncounterOutsideVisit`` before any write.
        """
        c = self._config
        if not c.note_marker_concept_uuid:
            raise ConfigError("bahmni: note_marker_concept_uuid is required to write ADT events")
        if not c.consultation_encounter_type_uuid:
            raise ConfigError("bahmni: consultation_encounter_type_uuid is required")
        concept_name = c.disposition_codes.get(kind)
        if not concept_name:
            raise ConfigError(f"bahmni: disposition_codes[{kind!r}] is required")
        note_concept = c.concept_map.get("disposition_note")
        if note is not None and not note_concept:
            raise ConfigError("bahmni: concept_map['disposition_note'] is required for a note")
        _check_uuid("visit_uuid", visit_uuid)
        marker = f"{_ADT_MARKER}{event_id}"
        visit, rows = await self._visit_and_encounters(visit_uuid, _MARKER_REP)
        for row in rows:
            found = self._has_marker(row, marker)
            if found:
                return RecordEncounterResult(encounter_uuid=found, created=False)
        self._check_in_visit(visit, at)
        sent = str(encounter_uuid or event_id)
        at_ms = to_epoch_ms(at)
        body = {
            "encounterUuid": sent,
            "patientUuid": patient_uuid,
            "visitUuid": visit_uuid,
            "locationUuid": c.location_uuid,
            "encounterTypeUuid": c.consultation_encounter_type_uuid,
            "encounterDateTime": at_ms,
            "providers": [{"uuid": provider_uuid}],
            "observations": [{"concept": {"uuid": c.note_marker_concept_uuid}, "value": marker}],
            "disposition": {
                "code": kind.upper(),
                "conceptName": concept_name,
                "dispositionDateTime": at_ms,
                "additionalObs": (
                    [] if note is None else [{"concept": {"uuid": note_concept}, "value": note}]
                ),
            },
        }
        resp = await self._post_encounter(_ENCOUNTER, body, None, at)
        if resp.status_code == 404:
            raise WriteNotSupported("bahmni: bahmniencounter route not found (HTTP 404)")
        parsed = resp.json() if resp.content else None
        return RecordEncounterResult(encounter_uuid=_sent_uuid(sent, parsed), created=True)

    # ─── Admit and transfer ──────────────────────────────────────────────────

    def _adt_observations(
        self, a: RecordAdtInput, encounter_type_uuid: str, type_key: str, *, bed: bool = True
    ) -> list[dict[str, Any]]:
        """Config and argument checks (before any HTTP), then the encounter's obs.

        ``bed=False`` (discharge) skips the bed fields: they are neither sent nor used.
        """
        c = self._config
        if not c.note_marker_concept_uuid:
            raise ConfigError("bahmni: note_marker_concept_uuid is required to write ADT events")
        if not c.ipd_visit_type_uuid:
            raise ConfigError("bahmni: ipd_visit_type_uuid is required for ADT writes")
        if not encounter_type_uuid:
            raise ConfigError(f"bahmni: {type_key} is required")
        note_concept = c.concept_map.get("adt_notes")
        if a.note is not None and not note_concept:
            raise ConfigError("bahmni: concept_map['adt_notes'] is required for a note")
        _check_uuid("patient_uuid", a.patient_uuid)
        if a.visit_uuid is not None:
            _check_uuid("visit_uuid", a.visit_uuid)
        if bed:
            if a.bed_uuid:  # informational only; shape-checked when given
                _check_uuid("bed_uuid", a.bed_uuid)
            if not isinstance(a.bed_id, int) or isinstance(a.bed_id, bool) or a.bed_id <= 0:
                raise ValueError(f"bahmni: bed_id is not a bed id: {a.bed_id!r}")
        obs: list[dict[str, Any]] = [
            {"concept": {"uuid": c.note_marker_concept_uuid}, "value": f"{_ADT_MARKER}{a.event_id}"}
        ]
        if a.note is not None:
            obs.append({"concept": {"uuid": note_concept}, "value": a.note})
        return obs

    def _adt_body(
        self, a: RecordAdtInput, encounter_type_uuid: str, observations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        # No encounterDateTime, as Bahmni's own ADT screen: the server stamps "now". A time
        # before today's midnight would send the write down Bahmni's retrospective path.
        return {
            "patientUuid": a.patient_uuid,
            "encounterTypeUuid": encounter_type_uuid,
            "visitTypeUuid": self._config.ipd_visit_type_uuid,
            "locationUuid": a.location_uuid,
            "providers": [{"uuid": a.provider_uuid}],
            "observations": observations,
        }

    async def _adt_visit(self, visit_uuid: str) -> dict[str, Any]:
        """One visit with its stop time, patient, type and each encounter's markers."""
        resp = await self._http.request(
            "GET", f"{_REST}/visit/{visit_uuid}", params={"v": _ADT_VISIT_REP}
        )
        if resp.status_code == 404:
            raise NotFound(f"bahmni: visit {visit_uuid} not found (HTTP 404)")
        body = resp.json() if resp.content else None
        if not isinstance(body, dict) or not isinstance(body.get("encounters"), list):
            raise VendorRejected("bahmni: visit response carried no encounter list")
        return body

    def _adt_marker(
        self, visit: dict[str, Any], encounter_type_uuid: str, marker: str
    ) -> str | None:
        """The encounter of ``encounter_type_uuid`` in ``visit`` carrying ``marker``."""
        for row in visit["encounters"]:
            if isinstance(row, dict) and row.get("voided") is True:
                # Bahmni's REST drops voided encounters; skip them should an install not.
                continue
            etype = row.get("encounterType") if isinstance(row, dict) else None
            if isinstance(etype, dict) and etype.get("uuid") == encounter_type_uuid:
                found = self._has_marker(row, marker)
                if found:
                    return found
        return None

    async def _bed_in_place(self, a: RecordAdtInput) -> RecordBedAssignment | RecordBed:
        """The patient's assignment when already in ``a.bed_id``, else the free target bed.

        The bed goes by ``a.bed_id`` only (``a.bed_uuid`` may be stale, see ``admit``).
        The patient's own bed is read first, so a target bed that is not AVAILABLE or
        lists a patient is held by someone else (or set unusable) ->
        ``ConflictError("bed_occupied")``.
        """
        current = await self.current_bed(a.patient_uuid)
        if current is not None and current.bed_id == a.bed_id:
            return current
        _, bed = await self._bed_by_id(a.bed_id)
        if bed.status != "AVAILABLE" or bed.patient_uuids:
            raise ConflictError("bed_occupied")
        return bed

    async def _assign_bed(
        self, a: RecordAdtInput, encounter_uuid: str, target: RecordBed
    ) -> RecordBedAssignment:
        """``POST beds/<bedId>``; Bahmni first unassigns the patient's current bed.

        ``target`` is the bed as read just before (its uuid as of now); assigning does
        not change the target's uuid, only the bed the patient leaves.
        """
        resp = await self._http.request(
            "POST",
            f"{_BEDS}/{a.bed_id}",
            json={"patientUuid": a.patient_uuid, "encounterUuid": encounter_uuid},
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: beds route not found (HTTP 404)")
        body = resp.json() if resp.content else None
        bed_id = _int(body.get("bedId")) if isinstance(body, dict) else None
        location = body.get("physicalLocation") if isinstance(body, dict) else None
        ward_uuid = location.get("uuid") if isinstance(location, dict) else None
        if not isinstance(body, dict) or bed_id != a.bed_id or not isinstance(ward_uuid, str):
            raise VendorRejected(
                "bahmni: bed assignment response did not name the bed", landed=True
            )
        bed_type = body.get("bedType")
        return RecordBedAssignment(
            bed_id=bed_id,
            bed_uuid=target.bed_uuid,
            bed_number=_text(body.get("bedNumber")),
            bed_type=_text(bed_type.get("name")) if isinstance(bed_type, dict) else "",
            ward_uuid=ward_uuid,
            ward_name=_label(location),
        )

    async def _post_adt(
        self, url: str, body: dict[str, Any], params: dict[str, str] | None = None
    ) -> tuple[str, str | None]:
        """(encounter uuid, visit uuid) from an ADT encounter write.

        These writes name no ``encounterUuid`` (the server stamps the time); a bare 500
        that Bahmni gives because two encounters match raises ``EncounterMatchAmbiguous``.
        """
        resp = await self._post_encounter(url, body, params, None)
        if resp.status_code == 404:
            if url in _NOT_FOUND_ROUTES:
                raise NotFound(f"bahmni: {_NOT_FOUND_ROUTES[url]} route not found (HTTP 404)")
            raise WriteNotSupported("bahmni: bahmniencounter route not found (HTTP 404)")
        parsed = resp.json() if resp.content else None
        uuid = parsed.get("encounterUuid") if isinstance(parsed, dict) else None
        if not isinstance(uuid, str) or not uuid:
            raise VendorRejected("bahmni: encounter response had no encounterUuid", landed=True)
        visit = parsed.get("visitUuid") if isinstance(parsed, dict) else None
        return uuid, visit if isinstance(visit, str) and visit else None

    async def admit(self, a: RecordAdtInput) -> RecordAdmitResult:
        """ADMISSION encounter + bed assignment; re-runnable with the same ``event_id``.

        - No open visit: an IPD visit is started (after the bed check, so a refused bed
          writes nothing).
        - An open non-IPD visit with ``admit_new_ipd_visit``: ``endVisitAndCreateEncounter``
          ends it and Bahmni creates the IPD visit. That body carries no ``visitUuid``:
          Bahmni would attach the encounter to the visit it has just ended.
        - Otherwise the ADMISSION goes into the open visit (``visitUuid``).

        When ``a.visit_uuid`` is given and no marker is found, it must be the open visit:
        a closed or different one raises ``ConflictError("visit_changed")`` before any
        write, including starting a visit. Re-read the open visit and resubmit the same
        ``event_id`` with it (or ``None`` when none is open). ``visit_uuid=None`` skips
        the guard; callers should always send the visit they saw. A patient already in
        ``a.bed_id`` with no marker for this event raises ``ConflictError("already_in_bed")``.

        A retry finds this event's ADMISSION by its marker in the open visit, or in the
        newest other visit (a converted visit since closed), and then only redoes the
        bed; ``visit_stopped`` is True when that visit has since closed. Bahmni may merge the ADMISSION into one of the same provider and visit
        inside its encounter session, so ``encounter_uuid`` can be shared; two matching
        encounters make Bahmni answer 500 (``TransientError``).

        The bed is ``a.bed_id``; ``a.bed_uuid`` is not used to find it, because Bahmni
        rewrites a bed's uuid whenever a patient leaves it (module docstring). An unknown
        bed id is ``NotFound`` before any write. With bahmni-ipd installed the login needs
        the ``Add Tasks`` and ``Edit Tasks`` privileges (nursing tasks are created on
        admission).
        """
        c = self._config
        obs = self._adt_observations(
            a, c.admission_encounter_type_uuid, "admission_encounter_type_uuid"
        )
        marker = f"{_ADT_MARKER}{a.event_id}"
        open_visit = await self.active_visit(a.patient_uuid)
        state = await self._adt_visit(open_visit.uuid) if open_visit else None
        converting = (
            state is not None
            and c.admit_new_ipd_visit
            and _ref_uuid(state.get("visitType")) != c.ipd_visit_type_uuid
        )
        visit_uuid = open_visit.uuid if open_visit else None
        visit_stopped = False
        found = self._adt_marker(state, c.admission_encounter_type_uuid, marker) if state else None
        if found is None and (state is None or converting):
            prior = next(
                (
                    v
                    for v in await self.list_visits(a.patient_uuid, limit=2)
                    if v.uuid != visit_uuid
                ),
                None,
            )
            if prior is not None:
                prior_state = await self._adt_visit(prior.uuid)
                found = self._adt_marker(prior_state, c.admission_encounter_type_uuid, marker)
                if found:
                    visit_uuid = prior.uuid
                    visit_stopped = prior_state.get("stopDatetime") is not None
        if found is None and a.visit_uuid is not None and a.visit_uuid != visit_uuid:
            # The visit the caller saw is no longer the open one (or none is open).
            raise ConflictError("visit_changed")
        held = await self._bed_in_place(a)
        in_place = held if isinstance(held, RecordBedAssignment) else None
        if found is None and in_place is not None:
            raise ConflictError("already_in_bed")
        created = new_visit = False
        if found is None:
            body = self._adt_body(a, c.admission_encounter_type_uuid, obs)
            if converting and open_visit is not None:
                found, new_uuid = await self._post_adt(
                    _END_AND_CREATE, body, {"visitUuid": open_visit.uuid}
                )
                if new_uuid is None:
                    raise VendorRejected("bahmni: admission response had no visitUuid", landed=True)
                visit_uuid, new_visit = new_uuid, True
            else:
                if visit_uuid is None:
                    started, new_visit = await self.start_visit(
                        a.patient_uuid, c.ipd_visit_type_uuid
                    )
                    visit_uuid = started.uuid
                found, _ = await self._post_adt(_ENCOUNTER, {**body, "visitUuid": visit_uuid})
            created = True
        if visit_uuid is None:  # unreachable: every branch above names the visit
            raise VendorRejected("bahmni: admission visit unknown", landed=created)
        bed = (
            held
            if isinstance(held, RecordBedAssignment)
            else await self._assign_bed(a, found, held)
        )
        return RecordAdmitResult(
            visit_uuid=visit_uuid,
            encounter_uuid=found,
            bed=bed,
            created_encounter=created,
            new_visit=new_visit,
            visit_stopped=visit_stopped,
        )

    async def transfer(self, a: RecordAdtInput) -> RecordAdmitResult:
        """TRANSFER encounter in ``a.visit_uuid`` + move to ``a.bed_id``; re-runnable.

        The visit must be the patient's and open, else ``ConflictError("visit_closed")``,
        also on a retry after the patient was discharged (the transfer is done or moot).
        Bahmni writes into a named visit even when stopped, so a discharge landing
        between the visit read and the write would file the TRANSFER in a closed visit;
        Callers serialise ADT writes per admission. Moving to the bed the patient
        already holds, with no marker for this event, raises
        ``ConflictError("already_in_bed")``. Bahmni unassigns the old bed itself when
        assigning the new one. Two quick transfers by one provider can merge into one
        TRANSFER encounter.
        """
        obs = self._adt_observations(
            a, self._config.transfer_encounter_type_uuid, "transfer_encounter_type_uuid"
        )
        if a.visit_uuid is None:
            raise ValueError("bahmni: transfer needs visit_uuid (the admission's visit)")
        state = await self._adt_visit(a.visit_uuid)
        if _ref_uuid(state.get("patient")) != a.patient_uuid:
            raise ValueError(f"bahmni: visit_uuid {a.visit_uuid} belongs to another patient")
        if state.get("stopDatetime") is not None:
            raise ConflictError("visit_closed")
        type_uuid = self._config.transfer_encounter_type_uuid
        found = self._adt_marker(state, type_uuid, f"{_ADT_MARKER}{a.event_id}")
        held = await self._bed_in_place(a)
        in_place = held if isinstance(held, RecordBedAssignment) else None
        if found is None and in_place is not None:
            raise ConflictError("already_in_bed")
        created = False
        if found is None:
            body = {**self._adt_body(a, type_uuid, obs), "visitUuid": a.visit_uuid}
            found, _ = await self._post_adt(_ENCOUNTER, body)
            created = True
        bed = (
            held
            if isinstance(held, RecordBedAssignment)
            else await self._assign_bed(a, found, held)
        )
        return RecordAdmitResult(
            visit_uuid=a.visit_uuid,
            encounter_uuid=found,
            bed=bed,
            created_encounter=created,
            new_visit=False,
            visit_stopped=False,
        )

    # ─── Discharge and end visit ─────────────────────────────────────────────

    async def discharge(
        self, a: RecordAdtInput, *, end_visit: bool = True
    ) -> RecordEncounterResult:
        """DISCHARGE encounter in ``a.visit_uuid`` (Bahmni frees the bed), then end the visit.

        Re-runnable with the same ``event_id``. This event's DISCHARGE marker is looked up
        first: when found nothing is re-posted (``created=False``) and the visit is still
        ended if it is open, so a retry after the visit ended is a success. With no
        marker, a stopped visit raises ``ConflictError("visit_closed")``. The visit must
        be the patient's (``ValueError``). ``a.bed_id``/``a.bed_uuid`` are not used:
        Bahmni unassigns the patient's current bed itself. No ``encounterDateTime`` is
        sent; Bahmni stamps "now". ``end_visit=False`` leaves the visit open (Bahmni
        then closes it with its stale-visit task). Bahmni may merge the DISCHARGE into
        one of the same provider and visit inside its encounter session, so
        ``encounter_uuid`` can be shared. ``endVisit`` acts on the stop state read at the
        start of this call (one visit read); a stale-visit close landing in between would
        have its stop time overwritten, a negligible window.

        With ``end_visit=True`` a successful return means the visit is stopped (already,
        or by this call). Two concurrent calls with the same event can both post; the caller
        serialises discharge per admission. A discharge after an undo-discharge needs a
        NEW ``event_id``: the voided DISCHARGE (and its marker) is dropped from the visit
        read, so the old id would either re-post or raise ``visit_closed``.
        """
        type_uuid = self._config.discharge_encounter_type_uuid
        obs = self._adt_observations(a, type_uuid, "discharge_encounter_type_uuid", bed=False)
        if a.visit_uuid is None:
            raise ValueError("bahmni: discharge needs visit_uuid (the admission's visit)")
        state = await self._adt_visit(a.visit_uuid)
        if _ref_uuid(state.get("patient")) != a.patient_uuid:
            raise ValueError(f"bahmni: visit_uuid {a.visit_uuid} belongs to another patient")
        stopped = state.get("stopDatetime") is not None
        found = self._adt_marker(state, type_uuid, f"{_ADT_MARKER}{a.event_id}")
        created = False
        if found is None:
            if stopped:
                raise ConflictError("visit_closed")
            body = {**self._adt_body(a, type_uuid, obs), "visitUuid": a.visit_uuid}
            found, _ = await self._post_adt(_DISCHARGE, body)
            created = True
        if end_visit and not stopped:
            # The stop state read above still holds: bahmnicore/discharge does not end it.
            await self._post_end_visit(a.visit_uuid)
        return RecordEncounterResult(encounter_uuid=found, created=created)

    async def end_visit(self, visit_uuid: str) -> bool:
        """End an open visit: ``True`` when this call ended it, ``False`` when already stopped.

        The visit is read first: Bahmni's ``endVisit`` on a stopped visit overwrites its
        stop time with "now", and on an unknown uuid answers 500. An unknown visit is
        ``NotFound``.
        """
        _check_uuid("visit_uuid", visit_uuid)
        state = await self._adt_visit(visit_uuid)
        if state.get("stopDatetime") is not None:
            return False
        await self._post_end_visit(visit_uuid)
        return True

    async def _post_end_visit(self, visit_uuid: str) -> None:
        resp = await self._http.request("POST", _END_VISIT, params={"visitUuid": visit_uuid})
        if resp.status_code == 404:
            raise NotFound("bahmni: endVisit route not found (HTTP 404)")
