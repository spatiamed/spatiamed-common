"""Bahmni radiology surface: orderable imaging procedures.

A standalone client over the shared ``BahmniHttp`` (auth, retry-on-401, error
mapping); it is not mixed into ``BahmniRecordClient`` and is imported by module path.

Orderable procedures are the members of one concept set (configured as
``orderable_set_uuid``); a member that is itself a set is expanded, depth first, in
member order. Bahmni's imaging bridge forwards an order to the imaging system only
when the procedure concept carries a mapping in the "PACS Procedure Code" concept
source; a procedure without one is reported with ``pacs_mapped=False`` because an
order for it would be dropped without any error.

Set members are read with an explicit custom representation. OpenMRS's custom-rep
parser breaks when anything follows ``))``, so the nested ``mappings`` group is last
and nested sets are read one ``GET concept/<uuid>`` per set rather than inline.

Imaging orders are written as their own ``bahmniencounter`` carrying only radiology
orders, under a client-generated ``encounterUuid`` (Bahmni honours it; a write that
names none fails once a visit holds two encounters of one type and provider). Each
placed order is read back for its order number, which the imaging bridge uses as the
accession number and refuses above 16 characters. Nothing here retries a write: a
caller that lost a response reconciles with ``find_radiology_orders`` before placing
again.

A typed report is one text obs of the configured report concept carrying the order's
``orderUuid`` (that link is how Bahmni and callers tie a report to its order), written
in its own encounter of the configured report encounter type. An amendment voids the
obs (``DELETE obs/<uuid>?reason=``) and writes a new one.

Image studies are not pushed into the record: ``GET pacs/studies`` runs a live study
query against the imaging system. HTTP 501 there means the imaging system is not
configured in Bahmni, a ``ConfigError`` coded ``pacs_not_configured`` (never an empty
list). Studies match orders by ``accession == order number``.

An outside film or scanned report is filed with Bahmni's two-step document upload:
the file first (``visitDocument/uploadDocument`` -> ``{url}``), then a document
encounter of the configured document encounter type into the caller's active visit.
Bahmni does not link these documents to an order; the caller keeps that link.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid as uuidlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from typing import Any, Literal

from sm_common.integrations.adapters.bahmni_http import BahmniHttp, to_epoch_ms
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    ConfigError,
    DocumentFilingFailed,
    HmsAdapterError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.radiology_types import (
    PacsStudy,
    PlacedRadiologyOrder,
    RadiologyDocumentRef,
    RadiologyOrderRow,
    RadiologyOrderWrite,
    RadiologyProcedure,
    RadiologyReportOutcome,
)

log = logging.getLogger(__name__)

_REST = "/openmrs/ws/rest/v1"
_CONCEPT = f"{_REST}/concept"
_ORDER = f"{_REST}/order"
_VISIT = f"{_REST}/visit"
_ENCOUNTER = f"{_REST}/bahmnicore/bahmniencounter"
# A Bahmni constant (the concept source its imaging bridge reads), not an identifier.
PACS_PROCEDURE_CODE_SOURCE = "PACS Procedure Code"
_SET_REP = (
    "custom:(setMembers:(uuid,display,set,"
    "mappings:(conceptReferenceTerm:(code,conceptSource:(name)))))"
)
# OpenMRS uuids are up to 38 chars of [A-Za-z0-9-]; anything else could reach another route.
_UUID_SHAPE = re.compile(r"[A-Za-z0-9-]{1,38}")
_MAX_LIMIT = 200
# One read per nested set; more than this is not an orderables list.
_MAX_SET_READS = 50
# The imaging bridge refuses an accession (the order number) over 16 bytes.
MAX_ACCESSION_BYTES = 16
_ORDER_READBACK_REP = "custom:(uuid,orderNumber,action,concept:(uuid),dateStopped)"
_ORDER_DISCONTINUE_REP = (
    "custom:(uuid,orderNumber,action,dateStopped,orderType:(uuid),"
    "careSetting:(careSettingType),concept:(uuid),autoExpireDate)"
)
# Encounters are read through the visit (``encounter?visit=`` without ``patient`` is a
# server error). The doubly nested group stays last: nothing may follow a "))".
_VISIT_ORDERS_REP = (
    "custom:(encounters:(orders:(uuid,orderNumber,action,dateActivated,dateStopped,"
    "orderType:(uuid),concept:(uuid))))"
)
_CARE_SETTINGS = ("OUTPATIENT", "INPATIENT")
_URGENCIES = ("ROUTINE", "STAT")
_MAX_COMMENT = 1024  # orders.comment_to_fulfiller
_MAX_REASON = 255  # orders.order_reason_non_coded
_TOO_LONG_REASON = "accession too long"

_OBS = f"{_REST}/obs"
_PACS_STUDIES = f"{_REST}/pacs/studies"
_DOC_UPLOAD = f"{_REST}/bahmnicore/visitDocument/uploadDocument"
_DOC_FILE = f"{_REST}/bahmnicore/visitDocument"
_MAX_REPORT_PART = 20_000  # findings, impression: each 1..20,000 chars
_MAX_VOID_REASON = 255  # obs.void_reason
_MAX_DOC_COMMENT = 1024
_MAX_FILE_NAME = 255
_MAX_IDENTIFIER = 64
# Bahmni's name for the radiology document encounter type; the upload step files the
# bytes under it (a constant, like the concept source name, not an identifier).
RADIOLOGY_DOCUMENT_ENCOUNTER_TYPE_NAME = "RADIOLOGY"
_ACCESSION_SYSTEM = "urn:bahmni:accession"
_OID_PREFIX = "urn:oid:"
# mime -> (format, fileType, leading magic bytes) for Bahmni's upload step.
_DOC_KINDS: dict[str, tuple[str, str, bytes]] = {
    "application/pdf": ("pdf", "pdf", b"%PDF"),
    "image/jpeg": ("jpeg", "image", b"\xff\xd8\xff"),
    "image/png": ("png", "image", b"\x89PNG\r\n\x1a\n"),
}
_FILE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_DICOM_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})")


def _refusal(code: str, *, landed: bool = False) -> VendorRejected:
    """A terminal refusal whose ``code`` (and message) is a stable machine-readable name."""
    exc = VendorRejected(code, landed=landed)
    exc.code = code
    return exc


class OrderNumberTooLong(VendorRejected):  # noqa: N818
    """An order number the imaging bridge would refuse as an accession. Terminal, landed.

    ``placed`` is every order the write created, in request order; ``too_long`` the
    ones whose number is over ``MAX_ACCESSION_BYTES``; ``discontinued`` the uuids of
    those actually discontinued (a failed discontinue is left out). Orders not in
    ``too_long`` stay active.
    """

    code = "order_number_too_long"

    def __init__(
        self,
        *,
        placed: list[PlacedRadiologyOrder],
        too_long: list[PlacedRadiologyOrder],
        discontinued: list[str],
    ) -> None:
        super().__init__("order_number_too_long", landed=True)
        self.placed = placed
        self.too_long = too_long
        self.discontinued = discontinued


@dataclass(frozen=True)
class RadiologyConfig:
    radiology_order_type_uuid: str = ""
    orderable_set_uuid: str = ""
    report_concept_uuid: str = ""
    report_encounter_type_uuid: str = ""
    document_encounter_type_uuid: str = ""

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> RadiologyConfig:
        """Build from an integration field mapping; unknown keys are ignored.

        No key is required here: each method checks the keys it needs before any I/O.
        """
        src = m or {}
        kwargs: dict[str, str] = {}
        for f in fields(cls):
            if f.name not in src or src[f.name] is None:
                continue
            value = src[f.name]
            if not isinstance(value, str):
                raise ConfigError(f"radiology config: {f.name} must be a string")
            kwargs[f.name] = value.strip()
        return cls(**kwargs)


def _str(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _check_uuid(name: str, value: Any) -> str:
    """Caller input check (ValueError) before the value enters a path or a body."""
    if not isinstance(value, str) or not _UUID_SHAPE.fullmatch(value):
        raise ValueError(f"radiology: {name} is not a uuid: {value!r}")
    return value


def _ref(v: Any) -> str | None:
    return _str(v.get("uuid")) if isinstance(v, dict) else None


def _pacs_code(mappings: Any) -> str | None:
    if not isinstance(mappings, list):
        return None
    for m in mappings:
        term = m.get("conceptReferenceTerm") if isinstance(m, dict) else None
        if not isinstance(term, dict):
            continue
        source = term.get("conceptSource")
        name = source.get("name") if isinstance(source, dict) else None
        if isinstance(name, str) and name.strip() == PACS_PROCEDURE_CODE_SOURCE:
            code = _str(term.get("code"))
            if code is not None:
                return code
    return None


def _report_part(name: str, value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > _MAX_REPORT_PART:
        raise ValueError(f"radiology: {name} must be 1-{_MAX_REPORT_PART} characters")
    return text


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"radiology: {name} must be a timezone-aware datetime")
    return value


def _study_at(raw: Any) -> datetime | None:
    """A study's ``started``: a DICOM date (``YYYYMMDD``, read as midnight UTC) or ISO.

    Checked before ``when``, which would read eight digits as epoch milliseconds.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    m = _DICOM_DATE.fullmatch(text)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
        except ValueError:
            return None
    if text.isdigit():
        return None
    return when(text)


def _modality(raw: Any) -> str | None:
    """``modality`` as a code string, a coding, or a list of codings (first code)."""
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        raw = raw.get("code")
    return _str(raw)


def _parse_study(raw: Any) -> PacsStudy | None:
    """One study row; ``None`` when it carries no StudyInstanceUID (nothing to match)."""
    if not isinstance(raw, dict):
        return None
    uid = _str(raw.get("id"))
    if uid is None:
        return None
    accession: str | None = None
    for ident in raw.get("identifier") or []:
        if not isinstance(ident, dict) or ident.get("system") != _ACCESSION_SYSTEM:
            continue
        value = _str(ident.get("value"))
        if value is not None and value.startswith(_OID_PREFIX):
            value = _str(value[len(_OID_PREFIX) :])
        accession = value
        break
    return PacsStudy(
        study_instance_uid=uid,
        accession=accession,
        modality=_modality(raw.get("modality")),
        study_at=_study_at(raw.get("started")),
        description=_str(raw.get("description")),
    )


def _safe_file_name(raw: Any) -> str:
    """Bahmni's own file-name whitelist, applied before the bytes leave: no path parts."""
    text = raw.strip() if isinstance(raw, str) else ""
    name = _FILE_NAME_UNSAFE.sub("-", text).replace("__", "_")
    name = name.lstrip(".-")
    if not name or len(name) > _MAX_FILE_NAME or set(name) <= {".", "-", "_"}:
        raise ValueError(f"radiology: file_name is empty or unusable: {raw!r}")
    return name


class BahmniRadiologyClient:
    def __init__(self, http: BahmniHttp, config: RadiologyConfig) -> None:
        self._http = http
        self._cfg = config

    def _require(self, key: str) -> str:
        value: str = getattr(self._cfg, key)
        if not value:
            raise ConfigError(f"radiology config missing required key: {key}")
        if not _UUID_SHAPE.fullmatch(value):
            raise ConfigError(f"radiology config: {key} is not a uuid")
        return value

    async def _set_members(self, set_uuid: str) -> list[dict[str, Any]]:
        resp = await self._http.request(
            "GET", f"{_CONCEPT}/{set_uuid}", params={"v": _SET_REP}, expect=(200,)
        )
        if resp.status_code == 404:
            raise NotFound(f"bahmni: concept set {set_uuid} not found (HTTP 404)")
        try:
            body = resp.json()
        except ValueError as exc:
            raise VendorRejected("bahmni: concept set response is not JSON") from exc
        members = body.get("setMembers") if isinstance(body, dict) else None
        if not isinstance(members, list) or not all(isinstance(m, dict) for m in members):
            raise VendorRejected(f"bahmni: concept {set_uuid} has no setMembers list")
        return members

    async def list_procedures(self, q: str = "", *, limit: int = 50) -> list[RadiologyProcedure]:
        """Orderable imaging procedures, nested sets flattened, filtered by name (case-insensitive)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"radiology: limit must be 1..{_MAX_LIMIT}")
        self._require("orderable_set_uuid")
        needle = q.strip().casefold()
        matched = [p for p in await self._all_procedures() if needle in p.name.casefold()]
        return matched[:limit]

    async def _all_procedures(self) -> list[RadiologyProcedure]:
        """Every orderable procedure, uncapped (the order check must see the whole set)."""
        root = self._require("orderable_set_uuid")
        procedures: list[RadiologyProcedure] = []
        seen: set[str] = {root}
        reads = 0

        async def walk(set_uuid: str) -> None:
            nonlocal reads
            reads += 1
            if reads > _MAX_SET_READS:
                raise VendorRejected(f"bahmni: orderable set nests more than {_MAX_SET_READS} sets")
            for member in await self._set_members(set_uuid):
                uuid = _str(member.get("uuid"))
                if uuid is None or not _UUID_SHAPE.fullmatch(uuid):
                    raise VendorRejected("bahmni: concept set member without a usable uuid")
                if uuid in seen:
                    continue
                seen.add(uuid)
                if member.get("set") is True:
                    await walk(uuid)
                    continue
                name = _str(member.get("display"))
                if name is None:
                    raise VendorRejected(f"bahmni: concept {uuid} has no display name")
                code = _pacs_code(member.get("mappings"))
                procedures.append(
                    RadiologyProcedure(
                        concept_uuid=uuid, name=name, pacs_code=code, pacs_mapped=code is not None
                    )
                )

        await walk(root)
        return procedures

    # ─── Orders ──────────────────────────────────────────────────────────────

    @staticmethod
    def _check_context(**kw: Any) -> None:
        for name, value in kw.items():
            _check_uuid(name, value)

    async def _post_encounter(
        self,
        *,
        patient_uuid: str,
        visit_uuid: str,
        encounter_type_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        orders: list[dict[str, Any]] | None = None,
        observations: list[dict[str, Any]] | None = None,
    ) -> tuple[str, Any]:
        """POST one new encounter carrying ``orders`` or ``observations``; returns
        (encounter uuid, parsed body).

        No ``encounterDateTime`` is sent: Bahmni stamps its own clock, so a caller's
        clock skew cannot put the encounter outside the visit.
        """
        sent = str(uuidlib.uuid4())
        body: dict[str, Any] = {
            "encounterUuid": sent,
            "patientUuid": patient_uuid,
            "visitUuid": visit_uuid,
            "locationUuid": location_uuid,
            "encounterTypeUuid": encounter_type_uuid,
            "providers": [{"uuid": provider_uuid}],
        }
        if orders is not None:
            body["orders"] = orders
        if observations is not None:
            body["observations"] = observations
        resp = await self._http.request("POST", _ENCOUNTER, json=body)
        if resp.status_code == 404:
            raise NotFound("bahmni: bahmniencounter route not found (HTTP 404)")
        try:
            parsed = resp.json() if resp.content else None
        except ValueError as exc:
            raise VendorRejected("bahmni: encounter response is not JSON", landed=True) from exc
        got = parsed.get("encounterUuid") if isinstance(parsed, dict) else None
        if got != sent:
            raise VendorRejected(
                "bahmni: encounter response did not name the encounterUuid sent", landed=True
            )
        return sent, parsed

    async def _read_order(self, order_uuid: str, rep: str) -> dict[str, Any] | None:
        resp = await self._http.request("GET", f"{_ORDER}/{order_uuid}", params={"v": rep})
        if resp.status_code == 404:
            return None
        try:
            body = resp.json()
        except ValueError as exc:
            raise VendorRejected("bahmni: order response is not JSON") from exc
        if not isinstance(body, dict):
            raise VendorRejected("bahmni: order response is not an object")
        return body

    async def _read_back(
        self, concepts: list[str], by_concept: dict[str, str], encounter_uuid: str
    ) -> list[PlacedRadiologyOrder]:
        """Read each placed order back for its order number, in request order."""
        placed: list[PlacedRadiologyOrder] = []
        for concept in concepts:
            order_uuid = by_concept[concept]
            back = await self._read_order(order_uuid, _ORDER_READBACK_REP)
            if back is None:
                raise VendorRejected(
                    f"bahmni: placed order {order_uuid} not found on read-back", landed=True
                )
            number = _str(back.get("orderNumber"))
            if back.get("uuid") != order_uuid or _ref(back.get("concept")) != concept or not number:
                raise VendorRejected(
                    f"bahmni: read-back of order {order_uuid} does not match the order placed",
                    landed=True,
                )
            placed.append(
                PlacedRadiologyOrder(
                    order_uuid=order_uuid,
                    order_number=number,
                    concept_uuid=concept,
                    encounter_uuid=encounter_uuid,
                )
            )
        return placed

    async def place_radiology_orders(
        self,
        *,
        patient_uuid: str,
        visit_uuid: str,
        encounter_type_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        care_setting: Literal["OUTPATIENT", "INPATIENT"],
        orders: Sequence[RadiologyOrderWrite],
        allow_unmapped: bool = False,
    ) -> list[PlacedRadiologyOrder]:
        """Place imaging orders in one new encounter; returned in request order.

        Every concept must be in the orderable set (else ``procedure_not_orderable``)
        and carry a "PACS Procedure Code" mapping unless ``allow_unmapped`` (else
        ``procedure_not_pacs_mapped``); both are refused before any write. Each placed
        order is read back for its order number; one over 16 bytes (UTF-8, which for
        the ASCII order numbers Bahmni issues is 16 characters) is discontinued (best
        effort) and ``OrderNumberTooLong`` (code ``order_number_too_long``) raised with
        ``landed=True``, carrying every placed order; the others stay active. Any
        error once the write is answered (a failed read-back included, even a
        ``TransientError``) carries ``landed=True``: the orders exist, and when the
        read-back failed the accession check has not run. Never retried: after a lost
        response or a landed error, reconcile with ``find_radiology_orders``.
        """
        self._check_context(
            patient_uuid=patient_uuid,
            visit_uuid=visit_uuid,
            encounter_type_uuid=encounter_type_uuid,
            provider_uuid=provider_uuid,
            location_uuid=location_uuid,
        )
        if care_setting not in _CARE_SETTINGS:
            raise ValueError(f"radiology: care_setting must be one of {_CARE_SETTINGS}")
        if not orders:
            raise ValueError("radiology: orders is empty")
        bodies: list[dict[str, Any]] = []
        concepts: list[str] = []
        order_type = self._require("radiology_order_type_uuid")
        self._require("orderable_set_uuid")
        for o in orders:
            concept = _check_uuid("concept_uuid", o.concept_uuid)
            if concept in concepts:
                # Bahmni refuses two active orders for one concept.
                raise ValueError(f"radiology: concept {concept} is ordered twice")
            if o.urgency not in _URGENCIES:
                raise ValueError(f"radiology: urgency must be one of {_URGENCIES}")
            comment = o.comment_to_fulfiller
            if comment is not None and not isinstance(comment, str):
                raise ValueError("radiology: comment_to_fulfiller must be text")
            comment = comment.strip() if comment else ""
            if len(comment) > _MAX_COMMENT:
                raise ValueError(f"radiology: comment_to_fulfiller is over {_MAX_COMMENT} chars")
            body: dict[str, Any] = {
                "concept": {"uuid": concept},
                "orderTypeUuid": order_type,
                "careSetting": care_setting,
                "urgency": o.urgency,
            }
            if comment:
                body["commentToFulfiller"] = comment
            concepts.append(concept)
            bodies.append(body)

        known = {p.concept_uuid: p for p in await self._all_procedures()}
        outside = [c for c in concepts if c not in known]
        if outside:
            log.warning("radiology: concepts not in the orderable set: %s", ", ".join(outside))
            raise _refusal("procedure_not_orderable")
        unmapped = [c for c in concepts if not known[c].pacs_mapped]
        if unmapped and not allow_unmapped:
            log.warning(
                "radiology: concepts without a PACS procedure code: %s", ", ".join(unmapped)
            )
            raise _refusal("procedure_not_pacs_mapped")

        context = {
            "patient_uuid": patient_uuid,
            "visit_uuid": visit_uuid,
            "encounter_type_uuid": encounter_type_uuid,
            "provider_uuid": provider_uuid,
            "location_uuid": location_uuid,
        }
        encounter_uuid, parsed = await self._post_encounter(**context, orders=bodies)

        by_concept: dict[str, str] = {}
        for row in parsed.get("orders") or [] if isinstance(parsed, dict) else []:
            if not isinstance(row, dict):
                continue
            concept = _ref(row.get("concept")) or _str(row.get("conceptUuid"))
            order_uuid = _str(row.get("uuid"))
            if concept in concepts and order_uuid and _UUID_SHAPE.fullmatch(order_uuid):
                by_concept.setdefault(concept, order_uuid)
        missing = [c for c in concepts if c not in by_concept]
        if missing:
            raise VendorRejected(
                "bahmni: encounter response is missing a placed radiology order", landed=True
            )

        try:
            placed = await self._read_back(concepts, by_concept, encounter_uuid)
        except HmsAdapterError as exc:
            # The orders are in the record: even a transient read-back failure must
            # not read as "safe to retry", or a retry would place them twice.
            exc.landed = True
            raise

        too_long = [p for p in placed if len(p.order_number.encode()) > MAX_ACCESSION_BYTES]
        if too_long:
            discontinued: list[str] = []
            for p in too_long:
                log.warning(
                    "radiology: order %s number is %d bytes (max %d); discontinuing it",
                    p.order_uuid,
                    len(p.order_number.encode()),
                    MAX_ACCESSION_BYTES,
                )
                try:
                    await self._discontinue(
                        **context,
                        order_uuid=p.order_uuid,
                        concept_uuid=p.concept_uuid,
                        care_setting=care_setting,
                        reason=_TOO_LONG_REASON,
                    )
                    discontinued.append(p.order_uuid)
                except HmsAdapterError as exc:
                    log.warning(
                        "radiology: discontinuing order %s failed: %s",
                        p.order_uuid,
                        type(exc).__name__,
                    )
            raise OrderNumberTooLong(placed=placed, too_long=too_long, discontinued=discontinued)
        return placed

    async def find_radiology_orders(self, visit_uuid: str) -> list[RadiologyOrderRow]:
        """Every radiology order of the visit (NEW and DISCONTINUE rows), oldest first."""
        _check_uuid("visit_uuid", visit_uuid)
        order_type = self._require("radiology_order_type_uuid")
        resp = await self._http.request(
            "GET", f"{_VISIT}/{visit_uuid}", params={"v": _VISIT_ORDERS_REP}
        )
        if resp.status_code == 404:
            raise NotFound(f"bahmni: visit {visit_uuid} not found (HTTP 404)")
        try:
            body = resp.json()
        except ValueError as exc:
            raise VendorRejected("bahmni: visit response is not JSON") from exc
        encounters = body.get("encounters") if isinstance(body, dict) else None
        if not isinstance(encounters, list):
            raise VendorRejected("bahmni: visit response carried no encounter list")
        rows: list[RadiologyOrderRow] = []
        for enc in encounters:
            for o in (enc.get("orders") if isinstance(enc, dict) else None) or []:
                if not isinstance(o, dict) or _ref(o.get("orderType")) != order_type:
                    continue
                order_uuid = _str(o.get("uuid"))
                number = _str(o.get("orderNumber"))
                concept = _ref(o.get("concept"))
                action = _str(o.get("action"))
                activated = when(o.get("dateActivated"))
                if not (order_uuid and number and concept and action and activated):
                    raise VendorRejected("bahmni: radiology order row is missing a field")
                rows.append(
                    RadiologyOrderRow(
                        order_uuid=order_uuid,
                        order_number=number,
                        concept_uuid=concept,
                        action=action,
                        date_activated=activated,
                        date_stopped=when(o.get("dateStopped")),
                    )
                )
        rows.sort(key=lambda r: r.date_activated)
        return rows

    async def _discontinue(
        self,
        *,
        order_uuid: str,
        concept_uuid: str,
        care_setting: str,
        reason: str,
        patient_uuid: str,
        visit_uuid: str,
        encounter_type_uuid: str,
        provider_uuid: str,
        location_uuid: str,
    ) -> None:
        await self._post_encounter(
            patient_uuid=patient_uuid,
            visit_uuid=visit_uuid,
            encounter_type_uuid=encounter_type_uuid,
            provider_uuid=provider_uuid,
            location_uuid=location_uuid,
            orders=[
                {
                    "concept": {"uuid": concept_uuid},
                    "orderTypeUuid": self._require("radiology_order_type_uuid"),
                    "careSetting": care_setting,
                    "action": "DISCONTINUE",
                    "previousOrderUuid": order_uuid,
                    "orderReasonText": reason,
                }
            ],
        )

    async def discontinue_radiology_order(
        self,
        *,
        order_uuid: str,
        patient_uuid: str,
        visit_uuid: str,
        encounter_type_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        reason: str,
    ) -> None:
        """Discontinue one radiology order in a new encounter; a stopped order is a no-op.

        An order past its ``autoExpireDate`` counts as stopped.

        The order is read first for its concept and care setting (a DISCONTINUE must
        repeat them); a non-radiology order raises ``not_a_radiology_order``.
        """
        self._check_context(
            order_uuid=order_uuid,
            patient_uuid=patient_uuid,
            visit_uuid=visit_uuid,
            encounter_type_uuid=encounter_type_uuid,
            provider_uuid=provider_uuid,
            location_uuid=location_uuid,
        )
        text = reason.strip() if isinstance(reason, str) else ""
        if not text or len(text) > _MAX_REASON:
            raise ValueError(f"radiology: reason must be 1-{_MAX_REASON} characters")
        order_type = self._require("radiology_order_type_uuid")
        order = await self._read_order(order_uuid, _ORDER_DISCONTINUE_REP)
        if order is None:
            raise NotFound(f"bahmni: order {order_uuid} not found (HTTP 404)")
        if _ref(order.get("orderType")) != order_type:
            raise _refusal("not_a_radiology_order")
        if order.get("dateStopped") is not None:
            return
        expires = when(order.get("autoExpireDate"))
        if expires is not None and expires <= datetime.now(timezone.utc):
            # Expired orders are no longer active; OpenMRS refuses to discontinue them.
            return
        if order.get("action") != "NEW":
            raise VendorRejected(
                f"bahmni: order {order_uuid} is a {order.get('action')!r} order, not discontinuable"
            )
        concept = _ref(order.get("concept"))
        setting = order.get("careSetting")
        care = setting.get("careSettingType") if isinstance(setting, dict) else None
        if concept is None or care not in _CARE_SETTINGS:
            raise VendorRejected(f"bahmni: order {order_uuid} read without concept or care setting")
        await self._discontinue(
            order_uuid=order_uuid,
            concept_uuid=concept,
            care_setting=care,
            reason=text,
            patient_uuid=patient_uuid,
            visit_uuid=visit_uuid,
            encounter_type_uuid=encounter_type_uuid,
            provider_uuid=provider_uuid,
            location_uuid=location_uuid,
        )

    # ─── Reports ─────────────────────────────────────────────────────────────

    async def write_radiology_report(
        self,
        *,
        order_uuid: str,
        patient_uuid: str,
        visit_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        report_at: datetime,
        findings: str,
        impression: str,
    ) -> RadiologyReportOutcome:
        """Write a signed report as one report-concept text obs linked to ``order_uuid``.

        The text is ``"Findings:\n" + findings + "\n\nImpression:\n" + impression``
        (each part stripped, 1-20,000 chars). The obs always carries ``orderUuid`` and
        ``observationDateTime = report_at``; the encounter is new (client-generated
        ``encounterUuid``) and stamped by the server. Every check runs before any I/O.
        Never retried: a ``TransientError`` may have landed, so a caller re-checks the
        record before writing again. Any failure after the write is answered carries
        ``landed=True``.
        """
        self._check_context(
            order_uuid=order_uuid,
            patient_uuid=patient_uuid,
            visit_uuid=visit_uuid,
            provider_uuid=provider_uuid,
            location_uuid=location_uuid,
        )
        at = to_epoch_ms(_aware("report_at", report_at))
        text = (
            "Findings:\n"
            + _report_part("findings", findings)
            + "\n\nImpression:\n"
            + _report_part("impression", impression)
        )
        concept = self._require("report_concept_uuid")
        encounter_type = self._require("report_encounter_type_uuid")
        encounter_uuid, parsed = await self._post_encounter(
            patient_uuid=patient_uuid,
            visit_uuid=visit_uuid,
            encounter_type_uuid=encounter_type,
            provider_uuid=provider_uuid,
            location_uuid=location_uuid,
            observations=[
                {
                    "concept": {"uuid": concept},
                    "orderUuid": order_uuid,
                    "observationDateTime": at,
                    "value": text,
                }
            ],
        )
        raw_obs = parsed.get("observations") if isinstance(parsed, dict) else None
        for obs in raw_obs if isinstance(raw_obs, list) else []:
            if not isinstance(obs, dict) or obs.get("voided") is True:
                continue
            obs_uuid = _str(obs.get("uuid"))
            if (
                obs_uuid is not None
                and _UUID_SHAPE.fullmatch(obs_uuid)
                and obs.get("orderUuid") == order_uuid
                and _ref(obs.get("concept")) == concept
            ):
                return RadiologyReportOutcome(encounter_uuid=encounter_uuid, obs_uuid=obs_uuid)
        raise VendorRejected(
            "bahmni: encounter response carried no report obs for the order", landed=True
        )

    async def void_radiology_report(self, obs_uuid: str, reason: str) -> None:
        """Void a report obs (for an amendment); OpenMRS voids, never purges.

        ``reason`` must be 1-255 characters and not blank. An unknown obs (HTTP 404)
        raises ``NotFound``.
        """
        _check_uuid("obs_uuid", obs_uuid)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _MAX_VOID_REASON:
            raise ValueError(f"radiology: reason must be 1-{_MAX_VOID_REASON} characters")
        resp = await self._http.request(
            "DELETE", f"{_OBS}/{obs_uuid}", params={"reason": reason}, expect=(200, 204)
        )
        if resp.status_code == 404:
            raise NotFound(f"bahmni: obs {obs_uuid} not found (HTTP 404)")

    # ─── Image studies ───────────────────────────────────────────────────────

    async def list_pacs_studies(
        self, patient_identifier: str, *, date: date | None = None
    ) -> list[PacsStudy]:
        """Studies the imaging system holds for a patient identifier (optionally one day).

        A live query through the record (not a stored copy); match a study to an order
        by ``accession == order_number``. ``date`` is sent as a DICOM date
        (``YYYYMMDD``); ``study_at`` is that study date at midnight UTC when the
        imaging system returns only a date. Rows without a StudyInstanceUID are
        skipped. HTTP 501 (imaging system not configured in the record) raises
        ``ConfigError`` coded ``pacs_not_configured``; any other 5xx (the query to
        the imaging system failed) stays a ``TransientError``; 404 is ``NotFound``.
        """
        ident = patient_identifier.strip() if isinstance(patient_identifier, str) else ""
        if not ident or len(ident) > _MAX_IDENTIFIER or any(not ch.isprintable() for ch in ident):
            raise ValueError("radiology: patient_identifier must be 1-64 printable characters")
        params = {"patientId": ident}
        if date is not None:
            params["date"] = date.strftime("%Y%m%d")
        try:
            resp = await self._http.request("GET", _PACS_STUDIES, params=params)
        except TransientError as exc:
            if exc.status_code == 501:
                err = ConfigError("pacs_not_configured")
                err.code = "pacs_not_configured"
                raise err from exc
            raise
        if resp.status_code == 404:
            raise NotFound("bahmni: pacs studies route not found (HTTP 404)")
        try:
            body = resp.json()
        except ValueError as exc:
            raise VendorRejected("bahmni: pacs studies response is not JSON") from exc
        if not isinstance(body, list):
            raise VendorRejected("bahmni: pacs studies response is not a list")
        studies: list[PacsStudy] = []
        skipped = 0
        for raw in body:
            study = _parse_study(raw)
            if study is None:
                skipped += 1
                continue
            studies.append(study)
        if skipped:
            log.warning("radiology: skipped %d study row(s) without a study uid", skipped)
        return studies

    # ─── Documents ───────────────────────────────────────────────────────────

    async def upload_radiology_document(
        self,
        *,
        patient_uuid: str,
        visit_uuid: str,
        visit_type_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        test_concept_uuid: str,
        content: bytes,
        mime_type: str,
        file_name: str,
        taken_at: datetime,
        comment: str | None = None,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> RadiologyDocumentRef:
        """File an outside film or scanned report into the active visit (two calls).

        Before any I/O: ``content`` must be 1..``max_bytes`` bytes of PDF, JPEG or PNG
        (checked by mime type and by its leading bytes), ``file_name`` is reduced to
        Bahmni's safe characters, ``taken_at`` is timezone-aware, ``comment`` is at
        most 1,024 characters. The upload step stores the bytes and answers a
        ``url``; the filing step attaches it under ``test_concept_uuid`` in a document
        encounter. With ``visit_uuid`` set Bahmni files into that visit and stamps the
        encounter itself, and may reuse an existing document encounter of the same
        type and provider, so ``encounter_uuid`` is not unique per upload. A filing
        failure raises ``DocumentFilingFailed`` carrying ``uploaded_url`` (``landed``
        when the encounter may exist). Not linked to the order: the caller keeps that.
        """
        self._check_context(
            patient_uuid=patient_uuid,
            visit_uuid=visit_uuid,
            visit_type_uuid=visit_type_uuid,
            provider_uuid=provider_uuid,
            location_uuid=location_uuid,
            test_concept_uuid=test_concept_uuid,
        )
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("radiology: max_bytes must be a positive integer")
        kind = _DOC_KINDS.get(mime_type) if isinstance(mime_type, str) else None
        if kind is None:
            raise ValueError(f"radiology: unsupported document mime {mime_type!r}")
        fmt, file_type, magic = kind
        if not isinstance(content, bytes) or not content:
            raise ValueError("radiology: empty document content")
        if len(content) > max_bytes:
            raise ValueError(f"radiology: document is over max_bytes ({max_bytes})")
        if not content.startswith(magic):
            raise ValueError(f"radiology: document content is not {mime_type}")
        name = _safe_file_name(file_name)
        at = to_epoch_ms(_aware("taken_at", taken_at))
        note = comment.strip() if isinstance(comment, str) else None
        if comment is not None and not isinstance(comment, str):
            raise ValueError("radiology: comment must be text")
        if note is not None and len(note) > _MAX_DOC_COMMENT:
            raise ValueError(f"radiology: comment is over {_MAX_DOC_COMMENT} characters")
        encounter_type = self._require("document_encounter_type_uuid")

        up = await self._http.request(
            "POST",
            _DOC_UPLOAD,
            json={
                "content": base64.b64encode(content).decode("ascii"),
                "format": fmt,
                "fileType": file_type,
                "patientUuid": patient_uuid,
                "encounterTypeName": RADIOLOGY_DOCUMENT_ENCOUNTER_TYPE_NAME,
                "fileName": name,
            },
        )
        if up.status_code == 404:
            raise NotFound("bahmni: uploadDocument route not found (HTTP 404)")
        try:
            uploaded = up.json() if up.content else None
        except ValueError as exc:
            raise VendorRejected("bahmni: document upload response is not JSON") from exc
        url = _str(uploaded.get("url")) if isinstance(uploaded, dict) else None
        if url is None:
            raise VendorRejected("bahmni: document upload response had no url")

        document: dict[str, Any] = {"testUuid": test_concept_uuid, "image": url, "obsDateTime": at}
        if note:
            document["comment"] = note
        try:
            resp = await self._http.request(
                "POST",
                _DOC_FILE,
                json={
                    "patientUuid": patient_uuid,
                    "visitUuid": visit_uuid,
                    "visitTypeUuid": visit_type_uuid,
                    # Ignored for an existing visit; Bahmni fails without a start date
                    # when it has to open one.
                    "visitStartDate": at,
                    "visitEndDate": at,
                    "encounterTypeUuid": encounter_type,
                    "encounterDateTime": at,
                    "providerUuid": provider_uuid,
                    "locationUuid": location_uuid,
                    "documents": [document],
                },
            )
        except (TransientError, VendorRejected, NotFound) as exc:
            # AuthError and ConfigError propagate unchanged.
            raise DocumentFilingFailed(
                f"bahmni: document uploaded but filing failed: {type(exc).__name__}",
                uploaded_url=url,
                cause=type(exc).__name__,
                landed=isinstance(exc, TransientError) or exc.landed,
            ) from exc
        if resp.status_code == 404:
            raise DocumentFilingFailed(
                "bahmni: visitDocument route not found (HTTP 404)",
                uploaded_url=url,
                cause="NotFound",
            )
        try:
            filed = resp.json() if resp.content else None
        except ValueError:
            filed = None
        encounter = _str(filed.get("encounterUuid")) if isinstance(filed, dict) else None
        if encounter is None:
            raise DocumentFilingFailed(
                "bahmni: document filing response had no encounterUuid",
                uploaded_url=url,
                cause="MalformedResponse",
                landed=True,
            )
        filed_visit = _str(filed.get("visitUuid")) if isinstance(filed, dict) else None
        if filed_visit is not None and filed_visit != visit_uuid:
            log.warning("radiology: document filed into visit %s, not the one sent", filed_visit)
        return RadiologyDocumentRef(encounter_uuid=encounter, file_url=url)
