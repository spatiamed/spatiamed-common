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
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.exceptions import ConfigError, NotFound, VendorRejected
from sm_common.integrations.radiology_types import RadiologyProcedure

_REST = "/openmrs/ws/rest/v1"
_CONCEPT = f"{_REST}/concept"
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
        root = self._require("orderable_set_uuid")
        needle = q.strip().casefold()

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
        matched = [p for p in procedures if needle in p.name.casefold()]
        return matched[:limit]
