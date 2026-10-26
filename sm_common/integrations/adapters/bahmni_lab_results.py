"""Bahmni lab results: read a patient's lab results from the hospital record.

A standalone client over the shared ``BahmniHttp`` (auth, re-login on 401, error
mapping); it is not part of ``BahmniRecordClient``. Import it by module path.

Read: ``GET bahmnicore/labOrderResults``, either ``?patientUuid=[&numberOfVisits=]``
or ``?visitUuids=a&visitUuids=b``. Bahmni answers ``{results: [...], tabularResult}``;
only ``results`` is used. Bahmni treats any top-level obs whose ``obs.order`` is a lab
order as that order's result, and lists an order that has no result obs yet as a row
with ``result: null`` and no result time; such rows parse as ``status="pending"``.
Bahmni's visit variant takes no patient uuid (it reads the patient of the first
visit), so a caller passing ``visit_uuids`` must pass visits of that one patient.

Test definitions: ``GET concept/<uuid>`` with a custom representation, one read per
distinct concept uuid.

Write body: results go into a ``bahmniencounter`` of the configured lab result
encounter type, with the obs tree Bahmni's own lab feed writes::

    single test:  Test -> Test -> [value, abnormal, min normal, max normal, notes, referred out]
    panel:        Panel -> [Test -> Test -> [...]] per member test

Every obs, at every level, carries the lab order uuid (for a panel, the panel order):
Bahmni links each obs to its order from that field, and its reader finds a result by
the top-level obs's order and then descends by concept.

Write guard: every write first reads ``labOrderResults`` for its visit and refuses
(``ConflictError`` with code ``record_result_newer``, nothing posted) when an order it
targets already holds a result at or after the new result time. A newer result in
the record is never overwritten; an amendment voids the old result group
(``void_lab_result``) and then writes afresh.

Void: ``DELETE obs/<uuid>?reason=`` voids (never purges) an obs; voiding a group obs
voids its members with it.

No environment reads and no default identifiers: every ``LabResultsConfig`` key
defaults to ``""`` and comes from the caller's integration mapping.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlencode

from sm_common.integrations.adapters.bahmni_http import BahmniHttp, to_epoch_ms
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import (
    ConfigError,
    ConflictError,
    NotFound,
    VendorRejected,
)
from sm_common.integrations.lab_results_types import (
    LabResultRow,
    LabResultWrite,
    LabResultWriteOutcome,
    LabTestDatatype,
    LabTestDefinition,
)

log = logging.getLogger(__name__)

_REST = "/openmrs/ws/rest/v1"
_LAB_ORDER_RESULTS = f"{_REST}/bahmnicore/labOrderResults"
_CONCEPT = f"{_REST}/concept"
_ENCOUNTER = f"{_REST}/bahmnicore/bahmniencounter"
_OBS = f"{_REST}/obs"
_MAX_VOID_REASON = 255
_TEST_DEFINITION_REP = "custom:(uuid,datatype:(name),units,answers:(uuid,display))"
# OpenMRS uuids are 36 characters, but older installs carry other shapes; this keeps
# anything that could escape a path segment or a query value out.
_UUID_SHAPE = re.compile(r"[A-Za-z0-9-]{1,38}")


@dataclass(frozen=True)
class LabResultsConfig:
    """Per-hospital lab result settings (encounter type, result concepts, location)."""

    lab_result_encounter_type_uuid: str = ""
    concept_lab_abnormal_uuid: str = ""
    concept_lab_minnormal_uuid: str = ""
    concept_lab_maxnormal_uuid: str = ""
    concept_lab_notes_uuid: str = ""
    concept_referred_out_uuid: str = ""
    result_location_uuid: str = ""

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> LabResultsConfig:
        """Build from an integration field mapping; unknown keys are ignored.

        A known key whose value is not a string is a ``ConfigError`` naming the key.
        """
        src = m or {}
        kwargs: dict[str, str] = {}
        for f in fields(cls):
            if f.name not in src:
                continue
            value = src[f.name]
            if not isinstance(value, str):
                raise ConfigError(f"bahmni lab results config: {f.name} must be a string")
            kwargs[f.name] = value
        return cls(**kwargs)


def _check_uuid(name: str, value: str) -> None:
    """Caller input check (ValueError) before the value enters a path or query."""
    if not isinstance(value, str) or not _UUID_SHAPE.fullmatch(value):
        raise ValueError(f"bahmni: {name} is not a uuid: {value!r}")


def _text(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw if raw.strip() else None
    return None


def _result_text(raw: Any) -> str | None:
    """Bahmni's ``result``: a string, or a bare number on some installs."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        return str(raw) if math.isfinite(raw) else None
    return _text(raw)


def _decimal(raw: Any) -> Decimal | None:
    """A JSON number (or numeric string) -> Decimal via its text, never via float maths."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    if not isinstance(raw, (int, float, str)):
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _tristate(raw: Any) -> bool | None:
    if raw is True or raw is False:
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true"
    return None


def _parse_row(raw: Any) -> LabResultRow | None:
    """One ``results[]`` entry -> a row; ``None`` when it names no order or test."""
    if not isinstance(raw, Mapping):
        return None
    order_uuid = _text(raw.get("orderUuid"))
    test_uuid = _text(raw.get("testUuid"))
    if order_uuid is None or test_uuid is None:
        return None
    result = _result_text(raw.get("result"))
    result_at = when(raw.get("resultDateTime"))
    referred_out = _tristate(raw.get("referredOut")) is True
    uploaded = _text(raw.get("uploadedFileName"))
    # A pending order has no result obs: no value, no result time, nothing filed.
    resulted = result is not None or result_at is not None or referred_out or uploaded is not None
    status: Literal["pending", "resulted"] = "resulted" if resulted else "pending"
    return LabResultRow(
        order_uuid=order_uuid,
        accession_uuid=_text(raw.get("accessionUuid")),
        test_uuid=test_uuid,
        test_name=_text(raw.get("testName")) or "",
        panel_uuid=_text(raw.get("panelUuid")),
        panel_name=_text(raw.get("panelName")),
        unit=_text(raw.get("testUnitOfMeasurement")),
        status=status,
        result=result,
        result_uuid=_text(raw.get("resultUuid")),
        min_normal=_decimal(raw.get("minNormal")),
        max_normal=_decimal(raw.get("maxNormal")),
        abnormal=_tristate(raw.get("abnormal")),
        referred_out=referred_out,
        notes=_text(raw.get("notes")),
        provider=_text(raw.get("provider")),
        result_at=result_at,
        uploaded_file_name=uploaded,
    )


def _parse_definition(concept_uuid: str, body: Any) -> LabTestDefinition:
    if not isinstance(body, Mapping):
        raise VendorRejected("bahmni: concept response is not an object")
    datatype_raw = body.get("datatype")
    name = datatype_raw.get("name") if isinstance(datatype_raw, Mapping) else None
    datatype: LabTestDatatype = "Other"
    if name == "Numeric":
        datatype = "Numeric"
    elif name == "Text":
        datatype = "Text"
    elif name == "Coded":
        datatype = "Coded"
    elif name == "Boolean":
        datatype = "Boolean"
    answers: list[tuple[str, str]] = []
    raw_answers = body.get("answers")
    if isinstance(raw_answers, list):
        for answer in raw_answers:
            if not isinstance(answer, Mapping):
                continue
            uuid = _text(answer.get("uuid"))
            if uuid is None:
                continue
            answers.append((uuid, _text(answer.get("display")) or ""))
    return LabTestDefinition(
        concept_uuid=concept_uuid,
        datatype=datatype,
        units=_text(body.get("units")),
        answers=tuple(answers),
    )


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"bahmni: {name} must be a timezone-aware datetime")
    return value


def _need(cfg: LabResultsConfig, key: str) -> str:
    value: str = getattr(cfg, key)
    if not value:
        raise ConfigError(f"bahmni lab results config: {key} is required")
    return value


def _check_result(r: Any) -> None:
    """Caller input check for one ``LabResultWrite`` (ValueError)."""
    if not isinstance(r, LabResultWrite):
        raise ValueError("bahmni: results must be LabResultWrite values")
    _check_uuid("order_uuid", r.order_uuid)
    _check_uuid("test_concept_uuid", r.test_concept_uuid)
    if r.panel_concept_uuid is not None:
        _check_uuid("panel_concept_uuid", r.panel_concept_uuid)
    _aware("result_at", r.result_at)
    if r.value_kind == "numeric":
        if not isinstance(r.value, Decimal) or not r.value.is_finite():
            raise ValueError("bahmni: a numeric result value must be a finite Decimal")
    elif r.value_kind == "text":
        if not isinstance(r.value, str) or not r.value.strip():
            raise ValueError("bahmni: a text result value must be a non-blank string")
    elif r.value_kind == "coded":
        if not isinstance(r.value, str):
            raise ValueError("bahmni: a coded result value must be an answer concept uuid")
        _check_uuid("coded value", r.value)
    else:
        raise ValueError(f"bahmni: unknown value_kind {r.value_kind!r}")
    for name, bound in (("min_normal", r.min_normal), ("max_normal", r.max_normal)):
        if bound is not None and (not isinstance(bound, Decimal) or not bound.is_finite()):
            raise ValueError(f"bahmni: {name} must be a finite Decimal")
    if r.notes is not None and not isinstance(r.notes, str):
        raise ValueError("bahmni: notes must be a string")


def _coded(exc: ConflictError | NotFound, code: str) -> ConflictError | NotFound:
    """Tag a refusal with a stable machine-readable code (the message is the code)."""
    exc.code = code
    return exc


def _check_against_record(results: Sequence[LabResultWrite], rows: Sequence[LabResultRow]) -> None:
    """Refuse a write the record already answers at or after its result time.

    Rows are matched on the order uuid only: for a panel order, every member row
    counts, because a second result group for one order is what the reader cannot
    tell apart. A resulted row without a result time cannot be shown to be older, so
    it refuses too. An order absent from the visit's rows is ``NotFound``.
    """
    by_order: dict[str, list[LabResultRow]] = {}
    for row in rows:
        by_order.setdefault(row.order_uuid, []).append(row)
    for r in results:
        if r.order_uuid not in by_order:
            raise _coded(NotFound("lab_order_not_in_visit"), "lab_order_not_in_visit")
    for r in results:
        new_at = r.result_at
        for row in by_order[r.order_uuid]:
            if row.status != "resulted":
                continue
            if row.result_at is None or row.result_at >= new_at:
                raise _coded(ConflictError("record_result_newer"), "record_result_newer")


def _top_level_groups(
    body: Any, wanted: Mapping[str, str]
) -> tuple[str, dict[str, tuple[str, ...]]]:
    """(encounter uuid, unvoided top-level group obs uuids per written order).

    ``wanted`` maps each written order to its top-level concept (the panel concept
    for a panel order, else the test concept). Session merging can put other orders'
    groups, and voided groups, in the same encounter; those are skipped. Any failure
    here happens after Bahmni accepted the write, so it is raised with ``landed``.
    """
    if not isinstance(body, Mapping):
        raise VendorRejected("bahmni: encounter response is not an object", landed=True)
    encounter = _text(body.get("encounterUuid"))
    if encounter is None:
        raise VendorRejected("bahmni: encounter response had no encounterUuid", landed=True)
    raw_obs = body.get("observations")
    found: dict[str, list[str]] = {order: [] for order in wanted}
    if isinstance(raw_obs, list):
        for obs in raw_obs:
            if not isinstance(obs, Mapping) or obs.get("voided") is True:
                continue
            order = _text(obs.get("orderUuid"))
            concept = obs.get("concept")
            concept_uuid = _text(concept.get("uuid")) if isinstance(concept, Mapping) else None
            uuid = _text(obs.get("uuid"))
            if order is None or uuid is None or order not in wanted:
                continue
            if concept_uuid != wanted[order]:
                continue
            found[order].append(uuid)
    missing = [order for order, uuids in found.items() if not uuids]
    if missing:
        raise VendorRejected(
            f"bahmni: encounter response carried no result group for {len(missing)} order(s)",
            landed=True,
        )
    return encounter, {order: tuple(uuids) for order, uuids in found.items()}


def _number(value: Decimal) -> float:
    """Decimal -> JSON number via its text (no Decimal maths in float)."""
    return float(str(value))


def _obs(
    concept_uuid: str, order_uuid: str, at: int, *, value: Any = None, members: Any = None
) -> dict[str, Any]:
    obs: dict[str, Any] = {
        "concept": {"uuid": concept_uuid},
        "orderUuid": order_uuid,
        "observationDateTime": at,
    }
    if members is not None:
        obs["groupMembers"] = members
    else:
        obs["value"] = value
    return obs


def _test_group(cfg: LabResultsConfig, r: LabResultWrite) -> dict[str, Any]:
    """``Test -> Test -> [value, abnormal, min, max, notes, referred out]`` for one result."""
    order = r.order_uuid
    at = to_epoch_ms(r.result_at)
    value: Any
    if r.value_kind == "numeric" and isinstance(r.value, Decimal):
        value = _number(r.value)
    elif r.value_kind == "coded":
        value = {"uuid": r.value}
    else:
        value = r.value
    leaves = [_obs(r.test_concept_uuid, order, at, value=value)]
    if r.abnormal is True:
        leaves.append(_obs(_need(cfg, "concept_lab_abnormal_uuid"), order, at, value=True))
    # Ranges only for a numeric result with both bounds (Bahmni's lab feed does the same).
    if r.value_kind == "numeric" and r.min_normal is not None and r.max_normal is not None:
        leaves.append(
            _obs(_need(cfg, "concept_lab_minnormal_uuid"), order, at, value=_number(r.min_normal))
        )
        leaves.append(
            _obs(_need(cfg, "concept_lab_maxnormal_uuid"), order, at, value=_number(r.max_normal))
        )
    if r.notes is not None and r.notes.strip():
        leaves.append(_obs(_need(cfg, "concept_lab_notes_uuid"), order, at, value=r.notes))
    if r.referred_out is True:
        leaves.append(_obs(_need(cfg, "concept_referred_out_uuid"), order, at, value=True))
    inner = _obs(r.test_concept_uuid, order, at, members=leaves)
    return _obs(r.test_concept_uuid, order, at, members=[inner])


def _result_encounter_body(
    cfg: LabResultsConfig,
    *,
    patient_uuid: str,
    visit_uuid: str,
    provider_uuid: str,
    location_uuid: str,
    encounter_datetime: datetime,
    results: Sequence[LabResultWrite],
) -> dict[str, Any]:
    """The ``bahmniencounter`` body for a batch of lab results. Pure: no I/O.

    Results are grouped per order in first-seen order: a single-test order becomes
    one ``Test -> Test -> [...]`` group; members of a panel order (same order uuid and
    panel concept) share one panel obs, whose time is the latest member result time.
    ``location_uuid`` falls back to ``cfg.result_location_uuid``. A config key the
    body needs (encounter type, location, and each result concept actually written)
    raises ``ConfigError`` naming the key; bad caller input raises ``ValueError``.
    Datetimes go out as epoch milliseconds.
    """
    for name, value in (
        ("patient_uuid", patient_uuid),
        ("visit_uuid", visit_uuid),
        ("provider_uuid", provider_uuid),
    ):
        _check_uuid(name, value)
    encounter_at = to_epoch_ms(_aware("encounter_datetime", encounter_datetime))
    if isinstance(results, (str, bytes)) or not results:
        raise ValueError("bahmni: results must be a non-empty sequence")
    for r in results:
        _check_result(r)
    encounter_type = _need(cfg, "lab_result_encounter_type_uuid")
    location = location_uuid or _need(cfg, "result_location_uuid")
    _check_uuid("location_uuid", location)

    # order uuid -> (panel concept or None, its results), insertion-ordered.
    groups: dict[str, tuple[str | None, list[LabResultWrite]]] = {}
    for r in results:
        panel, members = groups.setdefault(r.order_uuid, (r.panel_concept_uuid, []))
        if panel != r.panel_concept_uuid:
            raise ValueError(f"bahmni: order {r.order_uuid} has conflicting panel concepts")
        if panel is None and members:
            raise ValueError(f"bahmni: single-test order {r.order_uuid} given more than once")
        if any(m.test_concept_uuid == r.test_concept_uuid for m in members):
            raise ValueError(f"bahmni: order {r.order_uuid} repeats test {r.test_concept_uuid}")
        members.append(r)

    observations: list[dict[str, Any]] = []
    for order, (panel, members) in groups.items():
        if panel is None:
            observations.append(_test_group(cfg, members[0]))
            continue
        latest = max(to_epoch_ms(m.result_at) for m in members)
        observations.append(
            _obs(panel, order, latest, members=[_test_group(cfg, m) for m in members])
        )
    return {
        "patientUuid": patient_uuid,
        "visitUuid": visit_uuid,
        "encounterTypeUuid": encounter_type,
        "encounterDateTime": encounter_at,
        "locationUuid": location,
        "providers": [{"uuid": provider_uuid}],
        "observations": observations,
        "orders": [],
        "drugOrders": [],
    }


class BahmniLabResultsClient:
    """Lab results in the hospital record, over the shared Bahmni HTTP core."""

    def __init__(self, http: BahmniHttp, config: LabResultsConfig) -> None:
        self._http = http
        self._config = config

    @property
    def config(self) -> LabResultsConfig:
        return self._config

    async def read_lab_results(
        self,
        patient_uuid: str,
        *,
        visit_uuids: Sequence[str] | None = None,
        number_of_visits: int | None = None,
    ) -> list[LabResultRow]:
        """The patient's lab result rows (pending and resulted), in Bahmni's order.

        ``visit_uuids`` and ``number_of_visits`` are mutually exclusive; with neither,
        Bahmni reads every visit. An unknown patient (HTTP 404) reads as ``[]``.
        Rows without an order or test uuid are skipped.
        """
        _check_uuid("patient_uuid", patient_uuid)
        if visit_uuids is not None and number_of_visits is not None:
            raise ValueError("bahmni: visit_uuids and number_of_visits are mutually exclusive")
        query: list[tuple[str, str]]
        if visit_uuids is not None:
            if isinstance(visit_uuids, str):
                raise ValueError("bahmni: visit_uuids must be a sequence of uuids")
            unique = list(dict.fromkeys(visit_uuids))
            if not unique:
                raise ValueError("bahmni: visit_uuids must not be empty")
            for uuid in unique:
                _check_uuid("visit_uuid", uuid)
            query = [("visitUuids", uuid) for uuid in unique]
        else:
            query = [("patientUuid", patient_uuid)]
            if number_of_visits is not None:
                if (
                    isinstance(number_of_visits, bool)
                    or not isinstance(number_of_visits, int)
                    or number_of_visits < 1
                ):
                    raise ValueError("bahmni: number_of_visits must be a positive integer")
                query.append(("numberOfVisits", str(number_of_visits)))
        # Repeated query keys do not fit BahmniHttp's ``params`` mapping, so the
        # (validated) query is encoded into the path.
        resp = await self._http.request("GET", f"{_LAB_ORDER_RESULTS}?{urlencode(query)}")
        if resp.status_code == 404:
            return []
        try:
            body = resp.json()
        except ValueError as exc:
            raise VendorRejected("bahmni: labOrderResults response is not JSON") from exc
        if not isinstance(body, Mapping):
            raise VendorRejected("bahmni: labOrderResults response is not an object")
        raw_rows = body.get("results")
        if raw_rows is None:
            return []
        if not isinstance(raw_rows, list):
            raise VendorRejected("bahmni: labOrderResults results is not a list")
        rows: list[LabResultRow] = []
        skipped = 0
        for raw in raw_rows:
            row = _parse_row(raw)
            if row is None:
                skipped += 1
                continue
            rows.append(row)
        if skipped:
            log.warning(
                "bahmni labOrderResults: skipped %d row(s) without order/test uuid", skipped
            )
        return rows

    async def read_test_definitions(self, concept_uuids: Sequence[str]) -> list[LabTestDefinition]:
        """Datatype, units and coded answers for each distinct test concept, in input order.

        An unknown concept (HTTP 404) raises ``NotFound``.
        """
        if isinstance(concept_uuids, str):
            raise ValueError("bahmni: concept_uuids must be a sequence of uuids")
        unique = list(dict.fromkeys(concept_uuids))
        for uuid in unique:
            _check_uuid("concept_uuid", uuid)
        out: list[LabTestDefinition] = []
        for uuid in unique:
            resp = await self._http.request(
                "GET", f"{_CONCEPT}/{uuid}", params={"v": _TEST_DEFINITION_REP}
            )
            if resp.status_code == 404:
                raise NotFound(f"bahmni: no concept {uuid}")
            try:
                body = resp.json()
            except ValueError as exc:
                raise VendorRejected("bahmni: concept response is not JSON") from exc
            out.append(_parse_definition(uuid, body))
        return out

    async def write_lab_results(
        self,
        *,
        patient_uuid: str,
        visit_uuid: str,
        provider_uuid: str,
        location_uuid: str,
        encounter_datetime: datetime,
        results: Sequence[LabResultWrite],
    ) -> LabResultWriteOutcome:
        """Write lab results into the record, never over a newer one.

        1. Build the body (every input and config check; ``ValueError`` /
           ``ConfigError`` before any I/O).
        2. Read the visit's ``labOrderResults``: an order not in it raises
           ``NotFound`` (code ``lab_order_not_in_visit``); an order already resulted
           at or after a new ``result_at`` raises ``ConflictError`` (code
           ``record_result_newer``). Nothing is posted in either case.
        3. POST the body to ``bahmniencounter`` once (no retries here: a 5xx is a
           ``TransientError`` for the caller to cap; a 404 is ``NotFound``).

        An older result in the record does not block the write. To replace it, void
        its group first (``void_lab_result``), then write. The outcome's
        ``encounter_uuid`` is not unique per write (Bahmni merges writes of one type,
        visit and provider within its session window into one encounter).
        """
        body = _result_encounter_body(
            self._config,
            patient_uuid=patient_uuid,
            visit_uuid=visit_uuid,
            provider_uuid=provider_uuid,
            location_uuid=location_uuid,
            encounter_datetime=encounter_datetime,
            results=results,
        )
        rows = await self.read_lab_results(patient_uuid, visit_uuids=[visit_uuid])
        _check_against_record(results, rows)
        wanted = {r.order_uuid: r.panel_concept_uuid or r.test_concept_uuid for r in results}
        resp = await self._http.request("POST", _ENCOUNTER, json=body)
        if resp.status_code == 404:
            raise NotFound("bahmni: bahmniencounter route not found (HTTP 404)")
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise VendorRejected("bahmni: encounter response is not JSON", landed=True) from exc
        encounter, groups = _top_level_groups(parsed, wanted)
        return LabResultWriteOutcome(encounter_uuid=encounter, obs_uuids_by_order=groups)

    async def void_lab_result(self, obs_uuid: str, reason: str) -> None:
        """Void one result obs (usually a top-level result group) with a reason.

        OpenMRS voids, never purges, and voiding a group voids its members. The
        reason must be 1-255 characters and not blank. An unknown obs (HTTP 404)
        raises ``NotFound``.
        """
        _check_uuid("obs_uuid", obs_uuid)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _MAX_VOID_REASON:
            raise ValueError(
                f"bahmni: void reason must be 1-{_MAX_VOID_REASON} characters and not blank"
            )
        resp = await self._http.request(
            "DELETE", f"{_OBS}/{obs_uuid}", params={"reason": reason}, expect=(200, 204)
        )
        if resp.status_code == 404:
            raise NotFound(f"bahmni: no obs {obs_uuid}")
