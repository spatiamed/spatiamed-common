"""Bahmni orders surface: a visit's drug and lab orders, and a patient's active orders.

Mixed into ``BahmniRecordClient`` so the record client module does not grow
further. Every call goes through the shared ``BahmniHttp`` (auth, retry-on-401,
error mapping). Reads, the lab test catalogue, the lab-order part of the
encounter write, and one write of its own: the order fulfiller-status mirror.

- A visit's orders come nested in ``GET visit/<uuid>`` (the encounters of the
  visit, each with its orders), like every other per-visit encounter read. That rep
  names Order fields only: Bahmni's lab orders are plain Orders, and a DrugOrder-only
  field there fails the whole read. Each returned drug order's drug fields come from
  its own ``GET order/<uuid>``.
- A patient's active orders come from ``GET order?patient=&careSetting=&t=``
  (``drugorder`` or ``testorder``). The care setting and kind are checked again on
  every returned row: the server filter is not relied on alone, and lab and
  radiology orders share the ``testorder`` resource.

Drug orders written by our encounter push are non-coded (``drugNonCoded`` plus the
install's "other drug" concept) with flexible dosing: dose, frequency and
instructions ride as JSON in the order's dosing instructions and the structured
``dose``/``frequency`` stay null. Those values are recovered from the JSON; a value
that does not parse is left null, never guessed.

The fulfiller-status mirror (``POST order/<uuid>/fulfillerdetails``) reads the order
first. That read makes a repeat write a no-op, tells a missing order (``NotFound``)
apart from a REST build without the sub-resource (``WriteNotSupported``), and gives
the order type, so an accession number is sent only for a drug order (the lab feed
owns a lab order's field, and an order of unknown type may be a lab order). A voided
or DISCONTINUE order is left alone. Comments are fixed codes, never free text.

The lab test catalogue is the configured orderable concept set: its ``Test`` (or
``LabTest``) and ``LabSet`` (panel) members, each panel's own members, and the members
of any ``Sample``-class member (which name the tests' sample type). Lab orders ride in the
consultation encounter's ``orders`` (coded concept, the configured lab order type,
OUTPATIENT); a concept with an unexpired OUTPATIENT lab order already active in the
visit is not sent again, since Bahmni refuses a second active order for the same
concept in the same care setting. Bahmni stamps every encounter order with an expiry
(the encounter session length) and treats an expired order as inactive, so a repeat
after that is sent. The expiry is honoured only in that duplicate check: the visit
order reads still list such an order as active (it is still pending collection).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, get_args

from sm_common.integrations.adapters.bahmni_adt import _UUID_SHAPE, _check_uuid, _has_next
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    ConfigError,
    NotFound,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.record_types import (
    CareSetting,
    DosingSource,
    FulfillerStatus,
    LabUrgency,
    OrderKind,
    RecordEncounterInput,
    RecordLabOrder,
    RecordLabTest,
    RecordOrder,
)

_REST = "/openmrs/ws/rest/v1"
_ORDER = f"{_REST}/order"
_MAX_ORDER_PAGES = 20
# OpenMRS's custom-representation parser breaks when anything follows a double
# close "))", so every doubly nested group sits last in its parent. Each group
# here is nested once; the order list (and the encounter in the search rep) is last.
#
# Fields of org.openmrs.Order only. Bahmni's lab orders are plain Orders, and a
# DrugOrder-only field in a rep that also reads one fails the whole read ("Unknown
# property ... on class org.openmrs.Order"), so the generic reps never name one.
_ORDER_FIELDS = (
    "uuid,orderType:(uuid,name),concept:(uuid,display),careSetting:(careSettingType),"
    "orderer:(uuid,display),dateActivated,dateStopped,autoExpireDate,action,previousOrder:(uuid),"
    "accessionNumber,fulfillerStatus,voided"
)
# Fields of org.openmrs.DrugOrder only: read per drug order, or where every row is one.
_DRUG_ORDER_FIELDS = (
    "drug:(uuid,display),drugNonCoded,dose,doseUnits:(display),frequency:(display),duration,"
    "durationUnits:(display),quantity,quantityUnits:(display),dosingInstructions"
)
# The top-level names of _DRUG_ORDER_FIELDS: what a per-order read may add to a row.
_DRUG_ORDER_KEYS: frozenset[str] = frozenset(
    {
        "drug",
        "drugNonCoded",
        "dose",
        "doseUnits",
        "frequency",
        "duration",
        "durationUnits",
        "quantity",
        "quantityUnits",
        "dosingInstructions",
    }
)
# Per encounter; ``_encounters`` nests it under the visit.
_VISIT_ORDERS_REP = f"custom:(uuid,orders:({_ORDER_FIELDS}))"
# ``GET order/<uuid>``: the order resource answers the DrugOrder, so its fields resolve.
_DRUG_ORDER_DETAIL_REP = f"custom:(uuid,{_DRUG_ORDER_FIELDS})"
_SEARCH_TYPE: dict[str, str] = {"drug": "drugorder", "lab": "testorder"}
_DEFAULT_TYPE_NAMES: dict[str, str] = {"drug": "drug order", "lab": "lab order"}
_CARE_SETTINGS: tuple[CareSetting, ...] = ("OUTPATIENT", "INPATIENT")
_PLAIN_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# Nested group last (see above).
_FULFILLER_REP = (
    "custom:(uuid,fulfillerStatus,fulfillerComment,accessionNumber,voided,action,"
    "orderType:(uuid,name))"
)
_FULFILLER_STATUSES: frozenset[str] = frozenset(get_args(FulfillerStatus))
# The caller's fixed codes; a not-collected reason is a short lower-case code.
_FULFILLER_COMMENT = re.compile(r"sm:(?:dispensed|partial|collected|not_collected:[a-z0-9_]{1,32})")
# ─── Lab catalogue and lab orders ────────────────────────────────────────────
_CONCEPT = f"{_REST}/concept"
_MEMBER = "uuid,display,retired,conceptClass:(name)"
# Set -> member -> member: the recursive group is last at every level (see above).
_LAB_SET_REP = f"custom:(uuid,setMembers:({_MEMBER},setMembers:({_MEMBER},setMembers:({_MEMBER}))))"
_TEST_CLASSES: frozenset[str] = frozenset({"test", "labtest"})
_PANEL_CLASS = "labset"
_SAMPLE_CLASS = "sample"
_LAB_TESTS_TTL_S = 600.0
_LAB_URGENCIES: frozenset[str] = frozenset(get_args(LabUrgency))
# orders.comment_to_fulfiller column width.
_LAB_COMMENT_MAX = 1024
_monotonic = time.monotonic


def _active_orders_rep(kind: OrderKind) -> str:
    """The order-search rep: ``t=drugorder`` answers DrugOrders only, so the drug fields
    ride along; ``t=testorder`` answers no DrugOrder, so they must not."""
    fields = f"{_ORDER_FIELDS},{_DRUG_ORDER_FIELDS}" if kind == "drug" else _ORDER_FIELDS
    return f"custom:({fields},encounter:(uuid,visit:(uuid)))"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _text(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _shown(v: Any) -> str | None:
    """A ``(display)``/``(uuid,display)`` group's display, or a bare string."""
    if isinstance(v, dict):
        return _text(v.get("display")) or _text(v.get("name"))
    return _text(v)


def _uuid(v: Any) -> str | None:
    return _text(v.get("uuid")) if isinstance(v, dict) else None


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _whole(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _administration_json(raw: Any) -> dict[str, Any] | None:
    """The dosing-instructions JSON object, or None when absent or unparseable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_dose(v: Any) -> tuple[float | None, str | None]:
    """(dose, dose_text): ``dose`` only when the text is a plain number."""
    if isinstance(v, bool):
        return None, None
    if isinstance(v, (int, float)):
        return float(v), str(v)
    text = _text(v)
    if text is None:
        return None, None
    return (float(text) if _PLAIN_NUMBER.fullmatch(text) else None), text


_Dosing = tuple[DosingSource, float | None, str | None, str | None, str | None]


def _dosing(raw: dict[str, Any]) -> _Dosing:
    """(source, dose, dose_text, frequency, instructions)."""
    admin = _administration_json(raw.get("dosingInstructions"))
    instructions = _text(admin.get("instructions")) if admin else None
    dose = _number(raw.get("dose"))
    frequency = _shown(raw.get("frequency"))
    if dose is not None or frequency is not None:
        return "structured", dose, None, frequency, instructions
    if admin is not None and any(k in admin for k in ("dose", "frequency", "instructions")):
        json_dose, dose_text = _json_dose(admin.get("dose"))
        return (
            "administration_json",
            json_dose,
            dose_text,
            _text(admin.get("frequency")),
            instructions,
        )
    return "none", None, None, None, None


def _active(o: RecordOrder) -> bool:
    return o.date_stopped is None and o.action.upper() != "DISCONTINUE"


def _concept_class(m: dict[str, Any]) -> str:
    return (_shown(m.get("conceptClass")) or "").casefold()


def _members(raw: Any) -> list[dict[str, Any]]:
    """A concept's live (non-retired) set members; a missing list is empty."""
    rows = raw.get("setMembers") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    return [m for m in rows if isinstance(m, dict) and m.get("retired") is not True]


def _lab_tests(set_body: dict[str, Any]) -> list[RecordLabTest]:
    """The orderable set's tests and panels in set order, each concept once."""
    out: dict[str, RecordLabTest] = {}

    def add(m: dict[str, Any], sample_type: str | None) -> None:
        cls = _concept_class(m)
        uuid, name = _uuid(m), _shown(m)
        if (cls not in _TEST_CLASSES and cls != _PANEL_CLASS) or uuid is None or name is None:
            return
        if uuid not in out:
            out[uuid] = RecordLabTest(
                concept_uuid=uuid,
                name=name,
                is_panel=cls == _PANEL_CLASS,
                sample_type=sample_type,
            )
        if cls == _PANEL_CLASS:
            for member in _members(m):
                if _concept_class(member) in _TEST_CLASSES:
                    add(member, sample_type)

    for m in _members(set_body):
        if _concept_class(m) == _SAMPLE_CLASS:
            for member in _members(m):
                add(member, _shown(m))
        else:
            add(m, None)
    return list(out.values())


def _check_lab_order(o: RecordLabOrder) -> None:
    """Caller input check (ValueError) before any I/O."""
    _check_uuid("lab order concept_uuid", o.concept_uuid)
    if o.urgency not in _LAB_URGENCIES:
        raise ValueError(f"bahmni: unknown lab order urgency {o.urgency!r}")
    if not isinstance(o.comment, str):
        raise ValueError("bahmni: lab order comment must be a string")
    if len(o.comment) > _LAB_COMMENT_MAX:
        raise ValueError(f"bahmni: lab order comment exceeds {_LAB_COMMENT_MAX} characters")


class BahmniOrdersMixin:
    _http: BahmniHttp
    _config: BahmniConfig
    # (monotonic time read, tests); per client instance, set on a successful read.
    _lab_tests_cache: tuple[float, list[RecordLabTest]] | None = None

    if TYPE_CHECKING:
        # Provided by BahmniRecordClient.
        async def _encounters(self, visit_uuid: str, rep: str) -> list[Any]: ...

    def _order_kind(self, order_type: Any) -> OrderKind:
        """A configured order type uuid decides; without one, the stock type name does."""
        c = self._config
        type_uuid = _uuid(order_type)
        name = (_shown(order_type) or "").casefold()
        configured: tuple[tuple[OrderKind, str], ...] = (
            ("drug", c.drug_order_type_uuid),
            ("lab", c.lab_order_type_uuid),
        )
        for kind, cfg_uuid in configured:
            if cfg_uuid:
                if type_uuid == cfg_uuid:
                    return kind
            elif name == _DEFAULT_TYPE_NAMES[kind]:
                return kind
        return "other"

    def _order(self, raw: Any, encounter_uuid: str, visit_uuid: str) -> RecordOrder | None:
        """One order row -> ``RecordOrder``; a voided or malformed row is skipped."""
        if not isinstance(raw, dict) or raw.get("voided") is True:
            return None
        uuid = _text(raw.get("uuid"))
        activated = when(raw.get("dateActivated"))
        care = raw.get("careSetting")
        care_type = _text(care.get("careSettingType")) if isinstance(care, dict) else None
        care_setting = next((s for s in _CARE_SETTINGS if s == (care_type or "").upper()), None)
        if uuid is None or activated is None or care_setting is None:
            return None
        non_coded = _text(raw.get("drugNonCoded"))
        drug = raw.get("drug")
        concept = raw.get("concept")
        display = non_coded or _shown(drug) or _shown(concept) or ""
        source, dose, dose_text, frequency, instructions = _dosing(raw)
        orderer = raw.get("orderer")
        return RecordOrder(
            order_uuid=uuid,
            kind=self._order_kind(raw.get("orderType")),
            display_name=display,
            # A non-coded drug's concept is the install's "other drug" placeholder.
            concept_uuid=None if non_coded else _uuid(concept),
            drug_uuid=_uuid(drug),
            encounter_uuid=encounter_uuid,
            visit_uuid=visit_uuid,
            care_setting=care_setting,
            dose=dose,
            dose_units=_shown(raw.get("doseUnits")),
            frequency=frequency,
            duration=_whole(raw.get("duration")),
            duration_units=_shown(raw.get("durationUnits")),
            quantity=_number(raw.get("quantity")),
            quantity_units=_shown(raw.get("quantityUnits")),
            instructions=instructions,
            orderer_provider_uuid=_uuid(orderer),
            orderer_name=_shown(orderer),
            date_activated=activated,
            date_stopped=when(raw.get("dateStopped")),
            action=_text(raw.get("action")) or "NEW",
            previous_order_uuid=_uuid(raw.get("previousOrder")),
            accession_number=_text(raw.get("accessionNumber")),
            fulfiller_status=_text(raw.get("fulfillerStatus")),
            dosing_source=source,
            dose_text=dose_text,
            auto_expire_date=when(raw.get("autoExpireDate")),
        )

    async def list_visit_orders(
        self,
        visit_uuid: str,
        *,
        kinds: frozenset[OrderKind] = frozenset({"drug", "lab"}),
        active_only: bool = True,
    ) -> list[RecordOrder]:
        """Every order of the visit's encounters, oldest first.

        ``active_only`` drops DISCONTINUE orders and stopped orders (a revised order is
        stopped by its revision). Voided orders are never returned. A missing visit
        raises ``NotFound``.

        The visit read names Order fields only (a DrugOrder-only field there fails the
        whole read once the visit holds a lab order). Each drug order that is returned
        then gets its drug, dose, duration, quantity and dosing fields from its own
        ``GET order/<uuid>``: one extra call per returned drug order, none for other
        kinds (a lab-only read makes none). A drug order gone by then (HTTP 404) is
        still returned, with those fields null, as ``get_visit`` does; a read naming
        another order raises ``VendorRejected``.
        """
        out: list[RecordOrder] = []
        for row in await self._encounters(visit_uuid, _VISIT_ORDERS_REP):
            encounter_uuid = _text(row.get("uuid")) if isinstance(row, dict) else None
            if encounter_uuid is None:
                continue
            for raw in row.get("orders") or []:
                o = self._order(raw, encounter_uuid, visit_uuid)
                if o is None or o.kind not in kinds or (active_only and not _active(o)):
                    continue
                if o.kind == "drug":
                    extra = await self._drug_order_fields(o.order_uuid)
                    o = self._order({**raw, **extra}, encounter_uuid, visit_uuid) or o
                out.append(o)
        return sorted(out, key=lambda o: (o.date_activated, o.order_uuid))

    async def _drug_order_fields(self, order_uuid: str) -> dict[str, Any]:
        """One drug order's DrugOrder-only fields; ``{}`` when the order is gone (404)."""
        if not _UUID_SHAPE.fullmatch(order_uuid):
            raise VendorRejected("bahmni: visit order uuid is not uuid-shaped")
        resp = await self._http.request(
            "GET", f"{_ORDER}/{order_uuid}", params={"v": _DRUG_ORDER_DETAIL_REP}
        )
        if resp.status_code == 404:
            return {}
        body = resp.json() if resp.content else None
        if not isinstance(body, dict) or body.get("uuid") != order_uuid:
            raise VendorRejected("bahmni: drug order read did not answer the order asked for")
        return {k: v for k, v in body.items() if k in _DRUG_ORDER_KEYS}

    async def list_active_orders(
        self,
        patient_uuid: str,
        *,
        care_setting: Literal["OUTPATIENT", "INPATIENT"],
        kind: OrderKind,
    ) -> list[RecordOrder]:
        """The patient's active orders of one kind in one care setting, oldest first.

        ``kind`` must be ``drug`` or ``lab`` (``ValueError`` otherwise). ``visit_uuid``
        is ``""`` for an order whose encounter has no visit.
        """
        search_type = _SEARCH_TYPE.get(kind)
        if search_type is None:
            raise ValueError(f"bahmni: active orders are listed for drug or lab, not {kind!r}")
        base = {
            "patient": patient_uuid,
            "careSetting": care_setting,
            "t": search_type,
            "v": _active_orders_rep(kind),
        }
        out: list[RecordOrder] = []
        seen = 0
        for _ in range(_MAX_ORDER_PAGES):
            params = dict(base, startIndex=str(seen)) if seen else base
            resp = await self._http.request("GET", _ORDER, params=params)
            if resp.status_code == 404:
                raise NotFound("bahmni: order route not found (HTTP 404)")
            body = resp.json() if resp.content else None
            rows = body.get("results") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise VendorRejected("bahmni: order search response carried no results list")
            seen += len(rows)
            for raw in rows:
                enc = raw.get("encounter") if isinstance(raw, dict) else None
                encounter_uuid = _uuid(enc)
                if encounter_uuid is None:
                    continue
                visit_uuid = _uuid(enc.get("visit")) if isinstance(enc, dict) else None
                o = self._order(raw, encounter_uuid, visit_uuid or "")
                if o and o.kind == kind and o.care_setting == care_setting and _active(o):
                    out.append(o)
            if not rows or not (isinstance(body, dict) and _has_next(body)):
                return sorted(out, key=lambda o: (o.date_activated, o.order_uuid))
        raise VendorRejected(f"bahmni: active orders exceed {_MAX_ORDER_PAGES} pages")

    async def set_fulfiller_status(
        self,
        order_uuid: str,
        status: FulfillerStatus,
        *,
        comment: str = "",
        accession_number: str | None = None,
    ) -> None:
        """Mirror an order's fulfilment status into the record.

        ``order_uuid`` must be uuid-shaped, ``comment`` is ``""`` or a fixed code
        (``sm:dispensed``, ``sm:partial``, ``sm:collected``,
        ``sm:not_collected:<reason_code>``); anything else, or an unknown status,
        raises ``ValueError`` before any I/O. The comment is always written: ``""``
        clears a comment set earlier (``sm:partial`` -> ``""`` when only the status
        changes), so the caller passes the code it means on every call.

        ``accession_number`` is sent only when given, and only for a drug order: for
        a lab order, or an order whose type is absent or not recognised as drug, it
        raises ``ValueError`` (the lab feed owns a lab order's accession).

        Writing the status, comment and accession the order already carries is a
        no-op, and so is any write to a voided or DISCONTINUE order. A missing order raises ``NotFound``; a REST build without
        ``fulfillerdetails`` (404 on the write) raises ``WriteNotSupported``, which is
        terminal: the caller turns the mirror off rather than retrying.
        """
        if status not in _FULFILLER_STATUSES:
            raise ValueError(f"bahmni: unknown fulfiller status {status!r}")
        if comment and not _FULFILLER_COMMENT.fullmatch(comment):
            raise ValueError("bahmni: fulfiller comment must be a fixed sm: code")
        _check_uuid("order_uuid", order_uuid)
        path = f"{_ORDER}/{order_uuid}"
        resp = await self._http.request("GET", path, params={"v": _FULFILLER_REP})
        if resp.status_code == 404:
            raise NotFound("bahmni: order not found (HTTP 404)")
        current = resp.json() if resp.content else None
        if not isinstance(current, dict):
            raise VendorRejected("bahmni: order read returned no object")
        # Fail closed: a missing or unrecognised type may be a site's lab type.
        if accession_number is not None and self._order_kind(current.get("orderType")) != "drug":
            raise ValueError("bahmni: an accession number is sent only for a drug order")
        action = (_text(current.get("action")) or "").upper()
        if current.get("voided") is True or action == "DISCONTINUE":
            return
        if (
            _text(current.get("fulfillerStatus")) == status
            and "fulfillerComment" in current
            and (_text(current.get("fulfillerComment")) or "") == comment
            and (
                accession_number is None
                or _text(current.get("accessionNumber")) == accession_number
            )
        ):
            return
        body: dict[str, Any] = {"fulfillerStatus": status, "fulfillerComment": comment}
        if accession_number is not None:
            body["accessionNumber"] = accession_number
        resp = await self._http.request(
            "POST", f"{path}/fulfillerdetails", json=body, expect=(200, 201, 204)
        )
        if resp.status_code == 404:
            raise WriteNotSupported("bahmni: order fulfillerdetails route not found (HTTP 404)")

    # ─── Lab catalogue and lab orders ────────────────────────────────────────

    async def list_lab_tests(self) -> list[RecordLabTest]:
        """The orderable lab tests and panels of ``lab_orderable_set_uuid``.

        Members of class ``Test`` (or ``LabTest``) or ``LabSet`` (panels) are listed,
        then each panel's test members; a ``Sample``-class member's tests and panels are listed with
        that sample's name as ``sample_type``. Retired concepts and other classes are
        skipped, and each concept appears once. Without the key (or with one that is
        not uuid-shaped) this raises ``ConfigError`` before any I/O; a missing set
        raises ``NotFound``. A successful read is cached for 10 minutes in this client
        instance only.
        """
        set_uuid = self._config.lab_orderable_set_uuid
        if not set_uuid:
            raise ConfigError("bahmni: lab_orderable_set_uuid is required to list lab tests")
        if not _UUID_SHAPE.fullmatch(set_uuid):
            raise ConfigError("bahmni: lab_orderable_set_uuid is not a uuid")
        now = _monotonic()
        cached = self._lab_tests_cache
        if cached is not None and now - cached[0] < _LAB_TESTS_TTL_S:
            return list(cached[1])
        resp = await self._http.request("GET", f"{_CONCEPT}/{set_uuid}", params={"v": _LAB_SET_REP})
        if resp.status_code == 404:
            raise NotFound("bahmni: lab orderable concept set not found (HTTP 404)")
        body = resp.json() if resp.content else None
        if not isinstance(body, dict) or not isinstance(body.get("setMembers"), list):
            raise VendorRejected("bahmni: lab orderable set read carried no member list")
        tests = _lab_tests(body)
        self._lab_tests_cache = (now, tests)
        return list(tests)

    def _check_lab_orders(self, e: RecordEncounterInput) -> None:
        """Config and input checks for ``e.lab_orders``, before any I/O."""
        if not e.lab_orders:
            return
        if not self._config.lab_order_type_uuid:
            raise ConfigError("bahmni: lab_order_type_uuid is required for lab orders")
        for o in e.lab_orders:
            _check_lab_order(o)

    async def _lab_order_bodies(
        self, e: RecordEncounterInput
    ) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """(``orders`` entries for the encounter, concepts skipped as already active).

        Reads the visit's active lab orders (only when there are lab orders to send)
        and drops concepts with an unexpired OUTPATIENT order there; a repeat within
        ``e.lab_orders`` keeps its first entry and is not reported as skipped.
        """
        if not e.lab_orders:
            return [], ()
        now = _utcnow()
        active = await self.list_visit_orders(e.visit_uuid, kinds=frozenset({"lab"}))
        blocking = {
            o.concept_uuid
            for o in active
            if o.concept_uuid
            and o.care_setting == "OUTPATIENT"
            and (o.auto_expire_date is None or o.auto_expire_date >= now)
        }
        sent: set[str] = set()
        skipped: list[str] = []
        out: list[dict[str, Any]] = []
        for o in e.lab_orders:
            if o.concept_uuid in blocking:
                if o.concept_uuid not in skipped:
                    skipped.append(o.concept_uuid)
                continue
            if o.concept_uuid in sent:
                continue
            sent.add(o.concept_uuid)
            body: dict[str, Any] = {
                "concept": {"uuid": o.concept_uuid},
                "orderTypeUuid": self._config.lab_order_type_uuid,
                "action": "NEW",
                "careSetting": "OUTPATIENT",
                "urgency": o.urgency,
            }
            if o.comment:
                body["commentToFulfiller"] = o.comment
            out.append(body)
        return out, tuple(skipped)
