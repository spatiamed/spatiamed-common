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
"""

from __future__ import annotations

import logging
import re
import uuid as uuidlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Literal

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    ConfigError,
    HmsAdapterError,
    NotFound,
    VendorRejected,
)
from sm_common.integrations.radiology_types import (
    PlacedRadiologyOrder,
    RadiologyOrderRow,
    RadiologyOrderWrite,
    RadiologyProcedure,
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
    "careSetting:(careSettingType),concept:(uuid))"
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

    async def _post_orders(
        self,
        *,
        patient_uuid: str,
        visit_uuid: str,
        encounter_type_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        orders: list[dict[str, Any]],
    ) -> tuple[str, Any]:
        """POST one encounter carrying only ``orders``; returns (encounter uuid, body).

        No ``encounterDateTime`` is sent: Bahmni stamps its own clock, so a caller's
        clock skew cannot put the encounter outside the visit.
        """
        sent = str(uuidlib.uuid4())
        body = {
            "encounterUuid": sent,
            "patientUuid": patient_uuid,
            "visitUuid": visit_uuid,
            "locationUuid": location_uuid,
            "encounterTypeUuid": encounter_type_uuid,
            "providers": [{"uuid": provider_uuid}],
            "orders": orders,
        }
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
        order is read back for its order number; one over 16 bytes is discontinued
        (best effort) and ``order_number_too_long`` raised with ``landed=True``. Any
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
            raise VendorRejected("procedure_not_orderable")
        unmapped = [c for c in concepts if not known[c].pacs_mapped]
        if unmapped and not allow_unmapped:
            log.warning(
                "radiology: concepts without a PACS procedure code: %s", ", ".join(unmapped)
            )
            raise VendorRejected("procedure_not_pacs_mapped")

        context = {
            "patient_uuid": patient_uuid,
            "visit_uuid": visit_uuid,
            "encounter_type_uuid": encounter_type_uuid,
            "provider_uuid": provider_uuid,
            "location_uuid": location_uuid,
        }
        encounter_uuid, parsed = await self._post_orders(**context, orders=bodies)

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
                except HmsAdapterError as exc:
                    log.warning(
                        "radiology: discontinuing order %s failed: %s",
                        p.order_uuid,
                        type(exc).__name__,
                    )
            raise VendorRejected("order_number_too_long", landed=True)
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
        await self._post_orders(
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
            raise VendorRejected("not_a_radiology_order")
        if order.get("dateStopped") is not None:
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
