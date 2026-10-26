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

No environment reads and no default identifiers: every ``LabResultsConfig`` key
defaults to ``""`` and comes from the caller's integration mapping.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlencode

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_parse import when
from sm_common.integrations.exceptions import ConfigError, NotFound, VendorRejected
from sm_common.integrations.lab_results_types import (
    LabResultRow,
    LabTestDatatype,
    LabTestDefinition,
)

log = logging.getLogger(__name__)

_REST = "/openmrs/ws/rest/v1"
_LAB_ORDER_RESULTS = f"{_REST}/bahmnicore/labOrderResults"
_CONCEPT = f"{_REST}/concept"
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
