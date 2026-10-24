"""Bahmni patient record client: search, read, marker-idempotent create, update."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

from sm_common.identity.dob import age_on
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp, from_epoch_ms, to_epoch_ms
from sm_common.integrations.adapters.bahmni_parse import birth_date as parse_birth_date
from sm_common.integrations.adapters.bahmni_parse import flag, gender
from sm_common.integrations.exceptions import (
    ConfigError,
    ConflictError,
    NotFound,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.record_types import (
    RecordAddress,
    RecordDiagnosis,
    RecordDocumentInput,
    RecordDocumentResult,
    RecordDrugOrder,
    RecordEncounterInput,
    RecordEncounterResult,
    RecordEncounterSummary,
    RecordPatient,
    RecordPatientInput,
    RecordPatientPatch,
    RecordService,
    RecordVisit,
    RecordVisitDetail,
)

_REST = "/openmrs/ws/rest/v1"
_PROFILE_WRITE = f"{_REST}/bahmnicore/patientprofile"
_SERVICES = f"{_REST}/appointmentService/all/default"
_ALLERGY = "/openmrs/ws/fhir2/R4/AllergyIntolerance"
_DOC_UPLOAD = f"{_REST}/bahmnicore/visitDocument/uploadDocument"
_DOC_FILE = f"{_REST}/bahmnicore/visitDocument"
# mime -> (format extension, Bahmni fileType)
_DOC_MIME = {
    "application/pdf": ("pdf", "pdf"),
    "image/jpeg": ("jpeg", "image"),
    "image/png": ("png", "image"),
}
_MARKER_PREFIX = "sm:"
_VISIT_REP = "custom:(uuid,visitType,startDatetime,stopDatetime,location)"
_ENCOUNTER_REP = (
    "custom:(uuid,encounterType:(display),encounterDatetime,"
    "encounterProviders:(provider:(display)),"
    "obs:(concept:(uuid,display),value,comment,"
    "groupMembers:(concept:(display),value,comment)),"
    "orders:(drugNonCoded,orderType:(display)))"
)
_MARKER_REP = "custom:(uuid,obs:(concept:(uuid),value))"
_FLEXIBLE_DOSING = (
    "org.openmrs.module.bahmniemrapi.drugorder.dosinginstructions.FlexibleDosingInstructions"
)
_DIAGNOSIS_GROUP = "visit diagnoses"
_DIAGNOSIS_TITLES = ("non-coded diagnosis", "coded diagnosis")
_OFFSET_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def _when(raw: Any) -> datetime | None:
    """OpenMRS REST datetime (ISO with a ``+0530``-style offset, or epoch ms) -> aware UTC."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        try:
            return from_epoch_ms(raw)
        except ValueError:
            return None
    if not isinstance(raw, str):
        return None
    text = _OFFSET_NO_COLON.sub(r"\1:\2", raw.strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _display(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("display") or v.get("name") or "")
    return "" if v is None else str(v)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _address(raw: Any) -> RecordAddress:
    a = raw if isinstance(raw, dict) else {}
    return RecordAddress(
        line1=str(a.get("address1") or ""),
        city_village=str(a.get("cityVillage") or ""),
        district=str(a.get("countyDistrict") or ""),
        state=str(a.get("stateProvince") or ""),
        pincode=str(a.get("postalCode") or ""),
    )


def _address_body(a: RecordAddress) -> dict[str, str]:
    return {
        "address1": a.line1,
        "cityVillage": a.city_village,
        "countyDistrict": a.district,
        "stateProvince": a.state,
        "postalCode": a.pincode,
    }


class BahmniRecordClient:
    def __init__(self, http: BahmniHttp, config: BahmniConfig) -> None:
        self._http = http
        self._config = config

    # ─── Read ────────────────────────────────────────────────────────────────

    def _from_search_hit(self, hit: dict[str, Any], attribute: str) -> RecordPatient | None:
        uuid = hit.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            return None
        attrs: Any = hit.get("customAttribute")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                attrs = None
        value = attrs.get(attribute) if isinstance(attrs, dict) else None
        is_phone = attribute == self._config.phone_attribute_name
        dob = parse_birth_date(hit.get("birthDate"))
        age = hit.get("age")
        return RecordPatient(
            uuid=uuid,
            identifier=str(hit.get("identifier") or ""),
            given=str(hit.get("givenName") or ""),
            family=str(hit.get("familyName") or ""),
            gender=gender(hit.get("gender")),
            birth_date=dob,
            birth_date_estimated=flag(hit.get("birthdateEstimated")),
            age_years=age if isinstance(age, int) else (age_on(dob, _today()) if dob else None),
            phone=value if is_phone and isinstance(value, str) else None,
            address=RecordAddress(city_village=str(hit.get("addressFieldValue") or "")),
            dead=bool(hit.get("deathDate")) or flag(hit.get("dead")),
        )

    async def _search_raw(
        self, params: dict[str, str], attribute: str, *, missing_ok: bool = True
    ) -> tuple[list[RecordPatient], list[dict[str, Any]], bool]:
        resp = await self._http.request(
            "GET",
            f"{_REST}/bahmni/search/patient",
            params={"q": "", "s": "byIdOrNameOrVillage", "patientAttributes": attribute, **params},
        )
        if resp.status_code == 404 and not missing_ok:
            raise WriteNotSupported("bahmni: patient search route not found (HTTP 404)")
        body = resp.json() if resp.status_code != 404 else {}
        page = [h for h in (body.get("pageOfResults") or []) if isinstance(h, dict)]
        total = body.get("totalCount")
        truncated = isinstance(total, int) and total > len(page)
        patients = [p for h in page if (p := self._from_search_hit(h, attribute)) is not None]
        return patients, page, truncated

    async def search(
        self, *, q: str = "", phone: str | None = None, identifier: str | None = None
    ) -> tuple[list[RecordPatient], bool]:
        """(hits, truncated). ``truncated``: the server holds more hits than it returned."""
        params: dict[str, str] = {"q": q}
        if phone:
            params["customAttribute"] = phone
        if identifier:
            params["identifier"] = identifier
        patients, _, truncated = await self._search_raw(params, self._config.phone_attribute_name)
        return patients, truncated

    def _from_profile(self, body: Any, uuid: str) -> RecordPatient | None:
        patient = body.get("patient") if isinstance(body, dict) else None
        if not isinstance(patient, dict):
            return None
        person = patient.get("person") or {}
        idents = [i for i in patient.get("identifiers") or [] if isinstance(i, dict)]
        preferred = next((i for i in idents if i.get("preferred")), idents[0] if idents else {})
        name = next((n for n in person.get("names") or [] if isinstance(n, dict)), {})
        addr = next((a for a in person.get("addresses") or [] if isinstance(a, dict)), {})
        phone = self._attribute(person, self._config.phone_attribute_name, "value")
        dob = parse_birth_date(person.get("birthdate"))
        return RecordPatient(
            uuid=str(patient.get("uuid") or uuid),
            identifier=str(preferred.get("identifier") or ""),
            given=str(name.get("givenName") or ""),
            family=str(name.get("familyName") or ""),
            gender=gender(person.get("gender")),
            birth_date=dob,
            birth_date_estimated=flag(person.get("birthdateEstimated")),
            age_years=age_on(dob, _today()) if dob else None,
            phone=phone if isinstance(phone, str) else None,
            address=_address(addr),
            dead=flag(person.get("dead")),
        )

    @staticmethod
    def _attribute(person: dict[str, Any], display: str, field: str) -> Any:
        for a in person.get("attributes") or []:
            if isinstance(a, dict) and (a.get("attributeType") or {}).get("display") == display:
                return a.get(field)
        return None

    async def _profile(self, uuid: str) -> tuple[RecordPatient, dict[str, Any]] | None:
        resp = await self._http.request(
            "GET", f"{_REST}/patientprofile/{uuid}", params={"v": "full"}
        )
        if resp.status_code == 404:
            return None
        body = resp.json()
        rec = self._from_profile(body, uuid)
        return None if rec is None else (rec, body["patient"]["person"] or {})

    async def get_patient(self, uuid: str) -> RecordPatient | None:
        found = await self._profile(uuid)
        return found[0] if found else None

    # ─── Create ──────────────────────────────────────────────────────────────

    def _marker_configured(self) -> bool:
        c = self._config
        if bool(c.marker_attribute_name) != bool(c.marker_attribute_type_uuid):
            raise ConfigError(
                "bahmni: marker_attribute_name and marker_attribute_type_uuid go together"
            )
        return bool(c.marker_attribute_name)

    async def _find_by_marker(self, value: str) -> list[RecordPatient]:
        name = self._config.marker_attribute_name
        # Fail closed: a missing route or a partial page must not read as "no earlier
        # create", or a retry would POST a duplicate.
        _, page, truncated = await self._search_raw(
            {"customAttribute": value}, name, missing_ok=False
        )
        if truncated:
            raise ConflictError(
                "bahmni: marker search returned a partial page; cannot tell whether "
                "an earlier create exists"
            )
        # The search is a LIKE match: keep only records whose marker is exactly ours.
        exact = []
        for hit in page:
            attrs: Any = hit.get("customAttribute")
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except ValueError:
                    continue
            if isinstance(attrs, dict) and attrs.get(name) == value:
                rec = self._from_search_hit(hit, name)
                if rec is not None:
                    exact.append(rec)
        return exact

    async def create_patient(self, p: RecordPatientInput) -> tuple[RecordPatient, bool]:
        """(patient, created). Idempotent on ``p.marker`` when a marker attribute is configured.

        A phone already held by other patients is fine (households share numbers);
        idempotency comes from the marker, never from phone uniqueness.
        """
        c = self._config
        use_marker = self._marker_configured()
        if p.phone and not c.phone_attribute_type_uuid:
            raise ConfigError("bahmni: phone_attribute_type_uuid is required to store a phone")
        marker_value = f"{_MARKER_PREFIX}{p.marker}"
        if use_marker:
            existing = await self._find_by_marker(marker_value)
            if len(existing) > 1:
                raise ConflictError(
                    f"{len(existing)} Bahmni patients already carry our marker; "
                    "a human must reconcile",
                    landed=True,
                )
            if existing:
                return existing[0], False

        attributes: list[dict[str, Any]] = []
        if p.phone:
            attributes.append(
                {"attributeType": {"uuid": c.phone_attribute_type_uuid}, "value": p.phone}
            )
        if use_marker:
            attributes.append(
                {"attributeType": {"uuid": c.marker_attribute_type_uuid}, "value": marker_value}
            )
        person: dict[str, Any] = {
            "names": [{"givenName": p.given, "familyName": p.family, "preferred": True}],
            "gender": p.gender,
            "birthdate": p.birth_date.isoformat(),
            "birthdateEstimated": p.birth_date_estimated,
            "attributes": attributes,
        }
        if p.address != RecordAddress():
            person["addresses"] = [_address_body(p.address)]
        body = {
            "patient": {
                "person": person,
                "identifiers": [
                    {
                        "identifierType": c.identifier_type_uuid,
                        "identifierSourceUuid": c.identifier_source_uuid,
                        "preferred": True,
                        "voided": False,
                    }
                ],
            },
            "relationships": [],
        }
        if c.identifier_prefix:
            body["patient"]["identifiers"][0]["identifierPrefix"] = c.identifier_prefix
        resp = await self._http.request("POST", _PROFILE_WRITE, json=body)
        if resp.status_code == 404:
            raise WriteNotSupported("bahmni: patientprofile route not found (HTTP 404)")
        patient = resp.json().get("patient") if resp.content else None
        uuid = patient.get("uuid") if isinstance(patient, dict) else None
        idents = patient.get("identifiers") if isinstance(patient, dict) else None
        ident = idents[0].get("identifier") if idents and isinstance(idents[0], dict) else None
        if not uuid or not ident:
            raise VendorRejected(
                "bahmni: create response had no patient uuid/identifier", landed=True
            )
        return (
            RecordPatient(
                uuid=str(uuid),
                identifier=str(ident),
                given=p.given,
                family=p.family,
                gender=p.gender,
                birth_date=p.birth_date,
                birth_date_estimated=p.birth_date_estimated,
                age_years=age_on(p.birth_date, _today()),
                phone=p.phone,
                address=p.address,
                dead=False,
            ),
            True,
        )

    # ─── Update ──────────────────────────────────────────────────────────────

    async def update_patient(self, uuid: str, patch: RecordPatientPatch) -> RecordPatient:
        """Send only the changed person fields; return the record as it now stands."""
        found = await self._profile(uuid)
        if found is None:
            raise VendorRejected(f"bahmni: patient {uuid} not found (HTTP 404)")
        current, person = found
        out: dict[str, Any] = {"uuid": uuid}
        merged = current
        if patch.given is not None or patch.family is not None:
            name = next((n for n in person.get("names") or [] if isinstance(n, dict)), {})
            given = current.given if patch.given is None else patch.given
            family = current.family if patch.family is None else patch.family
            entry: dict[str, Any] = {"givenName": given, "familyName": family}
            if name.get("uuid"):
                entry = {"uuid": name["uuid"], **entry}
            out["names"] = [entry]
            merged = replace(merged, given=given, family=family)
        if patch.gender is not None:
            out["gender"] = patch.gender
            merged = replace(merged, gender=patch.gender)
        if patch.birth_date is not None:
            out["birthdate"] = patch.birth_date.isoformat()
            merged = replace(
                merged,
                birth_date=patch.birth_date,
                age_years=age_on(patch.birth_date, _today()),
            )
        if patch.birth_date_estimated is not None:
            out["birthdateEstimated"] = patch.birth_date_estimated
            merged = replace(merged, birth_date_estimated=patch.birth_date_estimated)
        if patch.address is not None:
            addr = next((a for a in person.get("addresses") or [] if isinstance(a, dict)), {})
            entry = _address_body(patch.address)
            if addr.get("uuid"):
                entry = {"uuid": addr["uuid"], **entry}
            out["addresses"] = [entry]
            merged = replace(merged, address=patch.address)
        if patch.phone is not None:
            if not self._config.phone_attribute_type_uuid:
                raise ConfigError("bahmni: phone_attribute_type_uuid is required to store a phone")
            attr: dict[str, Any] = {
                "attributeType": {"uuid": self._config.phone_attribute_type_uuid},
                "value": patch.phone,
            }
            existing = self._attribute(person, self._config.phone_attribute_name, "uuid")
            if existing:
                attr = {"uuid": existing, **attr}
            out["attributes"] = [attr]
            merged = replace(merged, phone=patch.phone)
        if len(out) == 1:
            return current
        resp = await self._http.request(
            "POST",
            f"{_PROFILE_WRITE}/{uuid}",
            json={"patient": {"uuid": uuid, "person": out}},
        )
        if resp.status_code == 404:
            raise VendorRejected(f"bahmni: patient {uuid} not found (HTTP 404)")
        return merged

    # ─── Visits ──────────────────────────────────────────────────────────────

    def _visit(self, raw: Any) -> RecordVisit | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("uuid"), str):
            return None
        start = _when(raw.get("startDatetime"))
        if start is None:
            return None
        vt = raw.get("visitType")
        loc = raw.get("location")
        loc_uuid = loc.get("uuid") if isinstance(loc, dict) else None
        vt_name = _display(vt) or (vt.get("uuid") if isinstance(vt, dict) else "") or ""
        return RecordVisit(
            uuid=raw["uuid"],
            visit_type=str(vt_name),
            start=start,
            stop=_when(raw.get("stopDatetime")),
            location_uuid=str(loc_uuid or self._config.location_uuid),
        )

    async def _visits(self, patient_uuid: str, params: dict[str, str]) -> list[RecordVisit]:
        resp = await self._http.request(
            "GET",
            f"{_REST}/visit",
            params={"patient": patient_uuid, "v": _VISIT_REP, **params},
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: visit route not found (HTTP 404)")
        rows = resp.json().get("results") or []
        return [v for r in rows if (v := self._visit(r)) is not None]

    async def active_visit(self, patient_uuid: str) -> RecordVisit | None:
        visits = await self._visits(patient_uuid, {"includeInactive": "false"})
        active = [v for v in visits if v.stop is None]
        return max(active, key=lambda v: v.start) if active else None

    async def start_visit(
        self, patient_uuid: str, visit_type_uuid: str | None = None
    ) -> tuple[RecordVisit, bool]:
        """(visit, created). An already-active visit is reused, never doubled."""
        active = await self.active_visit(patient_uuid)
        if active is not None:
            return active, False
        type_uuid = visit_type_uuid or self._config.opd_visit_type_uuid
        if not type_uuid:
            raise ConfigError("bahmni: opd_visit_type_uuid is required to start a visit")
        resp = await self._http.request(
            "POST",
            f"{_REST}/visit",
            json={
                "patient": patient_uuid,
                "visitType": type_uuid,
                "location": self._config.location_uuid,
            },
        )
        if resp.status_code == 404:
            raise NotFound("bahmni: visit route not found (HTTP 404)")
        visit = self._visit(resp.json() if resp.content else None)
        if visit is None:
            raise VendorRejected("bahmni: visit create response had no usable visit", landed=True)
        return visit, True

    async def list_visits(self, patient_uuid: str, limit: int = 20) -> list[RecordVisit]:
        visits = await self._visits(patient_uuid, {"includeInactive": "true", "limit": str(limit)})
        return sorted(visits, key=lambda v: v.start, reverse=True)[:limit]

    @staticmethod
    def _diagnoses(obs: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
        out: list[tuple[str, str | None]] = []
        for o in obs:
            if _display(o.get("concept")).lower() != _DIAGNOSIS_GROUP:
                continue
            for m in o.get("groupMembers") or []:
                if isinstance(m, dict) and _display(m.get("concept")).lower() in _DIAGNOSIS_TITLES:
                    comment = m.get("comment") or o.get("comment")
                    out.append((_display(m.get("value")), str(comment) if comment else None))
        return out

    def _encounter_summary(self, raw: Any) -> RecordEncounterSummary | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("uuid"), str):
            return None
        at = _when(raw.get("encounterDatetime"))
        if at is None:
            return None
        obs_rows = [o for o in raw.get("obs") or [] if isinstance(o, dict)]
        providers = [p for p in raw.get("encounterProviders") or [] if isinstance(p, dict)]
        return RecordEncounterSummary(
            uuid=raw["uuid"],
            encounter_type=_display(raw.get("encounterType")),
            at=at,
            provider=_display(providers[0].get("provider")) if providers else "",
            obs=[
                (_display(o.get("concept")), _display(o.get("value")))
                for o in obs_rows
                if o.get("value") is not None
                and _display(o.get("concept")).lower() != _DIAGNOSIS_GROUP
            ],
            diagnoses=self._diagnoses(obs_rows),
            drugs=[
                str(d["drugNonCoded"])
                for d in raw.get("orders") or []
                if isinstance(d, dict) and d.get("drugNonCoded")
            ],
        )

    async def _encounters(self, visit_uuid: str, rep: str) -> list[Any]:
        resp = await self._http.request(
            "GET", f"{_REST}/encounter", params={"visit": visit_uuid, "v": rep}
        )
        if resp.status_code == 404:
            raise NotFound(f"bahmni: encounters for visit {visit_uuid} not found (HTTP 404)")
        body = resp.json()
        if any(
            isinstance(link, dict) and link.get("rel") == "next" for link in body.get("links") or []
        ):
            raise ConflictError(
                "bahmni: encounter list is paged; cannot tell whether a note was already filed"
            )
        return list(body.get("results") or [])

    async def get_visit(self, visit_uuid: str) -> RecordVisitDetail | None:
        resp = await self._http.request(
            "GET", f"{_REST}/visit/{visit_uuid}", params={"v": _VISIT_REP}
        )
        if resp.status_code == 404:
            return None
        visit = self._visit(resp.json())
        if visit is None:
            return None
        rows = await self._encounters(visit_uuid, _ENCOUNTER_REP)
        encounters = [e for r in rows if (e := self._encounter_summary(r)) is not None]
        return RecordVisitDetail(visit=visit, encounters=sorted(encounters, key=lambda e: e.at))

    # ─── Encounter write ─────────────────────────────────────────────────────

    @staticmethod
    def _diagnosis_body(d: RecordDiagnosis) -> dict[str, Any]:
        body: dict[str, Any] = {
            "freeTextAnswer": d.title,
            "order": "PRIMARY" if d.primary else "SECONDARY",
            "certainty": "CONFIRMED",
        }
        # Title is free text; the code rides in the comment so a later pass can attach
        # a coded concept without re-reading the clinical note.
        if d.code:
            body["comments"] = f"{d.code_system} {d.code}"
        return body

    def _drug_body(self, d: RecordDrugOrder) -> dict[str, Any]:
        return {
            "careSetting": "OUTPATIENT",
            "drugNonCoded": d.name,
            "concept": {"uuid": self._config.drug_other_concept_uuid},
            "dosingInstructionType": _FLEXIBLE_DOSING,
            "dosingInstructions": {
                "administrationInstructions": json.dumps(
                    {"dose": d.dose, "frequency": d.frequency, "instructions": d.instructions}
                )
            },
            "duration": d.duration_days,
            "durationUnits": "Days",
        }

    def _has_marker(self, row: Any, marker: str) -> str | None:
        if not isinstance(row, dict) or not isinstance(row.get("uuid"), str):
            return None
        for o in row.get("obs") or []:
            concept = o.get("concept") if isinstance(o, dict) else None
            if (
                isinstance(concept, dict)
                and concept.get("uuid") == self._config.note_marker_concept_uuid
                and o.get("value") == marker
            ):
                return str(row["uuid"])
        return None

    async def write_encounter(self, e: RecordEncounterInput) -> RecordEncounterResult:
        """File one clinical note as an encounter; idempotent on ``e.note_id``."""
        c = self._config
        if not c.note_marker_concept_uuid:
            raise ConfigError("bahmni: note_marker_concept_uuid is required to write encounters")
        if not c.consultation_encounter_type_uuid:
            raise ConfigError("bahmni: consultation_encounter_type_uuid is required")
        if e.drug_orders and not c.drug_other_concept_uuid:
            raise ConfigError("bahmni: drug_other_concept_uuid is required for drug orders")
        observations: list[dict[str, Any]] = []
        for o in e.obs:
            concept = c.concept_map.get(o.key)
            if not concept:
                raise ConfigError(f"concept_map missing {o.key}")
            observations.append({"concept": {"uuid": concept}, "value": o.value})
        marker = str(e.note_id)
        for row in await self._encounters(e.visit_uuid, _MARKER_REP):
            found = self._has_marker(row, marker)
            if found:
                return RecordEncounterResult(encounter_uuid=found, created=False)
        observations.append({"concept": {"uuid": c.note_marker_concept_uuid}, "value": marker})
        body = {
            "patientUuid": e.patient_uuid,
            "visitUuid": e.visit_uuid,
            "locationUuid": c.location_uuid,
            "encounterTypeUuid": c.consultation_encounter_type_uuid,
            "encounterDateTime": to_epoch_ms(e.encounter_at),
            "providers": [{"uuid": e.provider_uuid}],
            "observations": observations,
            "bahmniDiagnoses": [self._diagnosis_body(d) for d in e.diagnoses],
            "drugOrders": [self._drug_body(d) for d in e.drug_orders],
        }
        resp = await self._http.request("POST", f"{_REST}/bahmnicore/bahmniencounter", json=body)
        if resp.status_code == 404:
            raise WriteNotSupported("bahmni: bahmniencounter route not found (HTTP 404)")
        parsed = resp.json() if resp.content else None
        uuid = parsed.get("encounterUuid") if isinstance(parsed, dict) else None
        if not isinstance(uuid, str) or not uuid:
            raise VendorRejected("bahmni: encounter response had no encounterUuid", landed=True)
        return RecordEncounterResult(encounter_uuid=uuid, created=True)

    # ─── Services, allergies, documents ──────────────────────────────────────

    async def list_services(self) -> list[RecordService]:
        resp = await self._http.request("GET", _SERVICES)
        if resp.status_code == 404:
            raise NotFound("bahmni: appointmentService route not found (HTTP 404)")
        body = resp.json() if resp.content else None
        if not isinstance(body, list):
            raise VendorRejected("bahmni: appointment services response was not a list")
        out: list[RecordService] = []
        for row in body:
            if not isinstance(row, dict):
                continue
            uuid, name = row.get("uuid"), row.get("name")
            if not isinstance(uuid, str) or not uuid or not isinstance(name, str) or not name:
                continue
            mins = row.get("durationMins")
            ok = isinstance(mins, int) and not isinstance(mins, bool)
            out.append(RecordService(uuid, name, mins if ok else None))
        return out

    async def allergies(self, patient_uuid: str) -> list[str] | None:
        """Allergen names, or None when the FHIR2 route is absent (HTTP 404).

        Unverified, Task 9: standard OpenMRS FHIR2 shape, not yet proven live.
        An entry with no readable name is reported as "Unspecified allergen"
        rather than dropped, and a paged/truncated bundle raises.
        """
        resp = await self._http.request("GET", _ALLERGY, params={"patient": patient_uuid})
        if resp.status_code == 404:
            return None
        body = resp.json() if resp.content else None
        if not isinstance(body, dict) or body.get("resourceType") != "Bundle":
            raise VendorRejected("bahmni: allergy response was not a FHIR Bundle")
        links = body.get("link")
        if isinstance(links, list) and any(
            isinstance(x, dict) and x.get("relation") == "next" for x in links
        ):
            raise VendorRejected(
                "bahmni: allergy bundle truncated (paged), refusing a partial list"
            )
        names: list[str] = []
        for entry in body.get("entry") or []:
            res = entry.get("resource") if isinstance(entry, dict) else None
            if not isinstance(res, dict) or res.get("resourceType") != "AllergyIntolerance":
                continue
            names.append(self._allergen_name(res))
        return names

    @staticmethod
    def _allergen_name(res: dict[str, Any]) -> str:
        code = res.get("code")
        if isinstance(code, dict):
            text = code.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
            for c in code.get("coding") or []:
                disp = c.get("display") if isinstance(c, dict) else None
                if isinstance(disp, str) and disp.strip():
                    return disp.strip()
        return "Unspecified allergen"

    async def upload_document(self, d: RecordDocumentInput) -> RecordDocumentResult:
        """Upload a file then attach it to a document encounter (two calls).

        Unverified, Task 9: both payload shapes follow Bahmni's open-source
        frontend and have not been proven live. Not idempotent: a retry after a
        failure in the second call leaves an orphan uploaded file.
        """
        c = self._config
        concept = c.document_type_concepts.get(d.doc_type)
        if not concept:
            raise ConfigError(f"bahmni: document_type_concepts missing {d.doc_type!r}")
        if not c.document_visit_type_uuid or not c.document_encounter_type_uuid:
            raise ConfigError(
                "bahmni: document_visit_type_uuid and document_encounter_type_uuid are required"
            )
        kind = _DOC_MIME.get(d.mime)
        if kind is None:
            raise ValueError(f"bahmni: unsupported document mime {d.mime!r}")
        if not d.content:
            raise ValueError("bahmni: empty document content")
        ext, file_type = kind
        up = await self._http.request(
            "POST",
            _DOC_UPLOAD,
            json={
                "content": base64.b64encode(d.content).decode("ascii"),
                "format": ext,
                "patientUuid": d.patient_uuid,
                "encounterTypeName": "Patient Document",
                "fileType": file_type,
            },
        )
        if up.status_code == 404:
            raise WriteNotSupported("bahmni: uploadDocument route not found (HTTP 404)")
        parsed = up.json() if up.content else None
        url = parsed.get("url") if isinstance(parsed, dict) else None
        if not isinstance(url, str) or not url:
            raise VendorRejected("bahmni: document upload response had no url")
        at = to_epoch_ms(d.taken_at)
        resp = await self._http.request(
            "POST",
            _DOC_FILE,
            json={
                "patientUuid": d.patient_uuid,
                "visitTypeUuid": c.document_visit_type_uuid,
                "encounterTypeUuid": c.document_encounter_type_uuid,
                "providerUuid": d.provider_uuid,
                "locationUuid": c.location_uuid,
                "encounterDateTime": at,
                "documents": [{"testUuid": concept, "image": url, "obsDateTime": at}],
            },
        )
        if resp.status_code == 404:
            raise WriteNotSupported("bahmni: visitDocument route not found (HTTP 404)")
        out = resp.json() if resp.content else None
        visit = out.get("visitUuid") if isinstance(out, dict) else None
        enc = out.get("encounterUuid") if isinstance(out, dict) else None
        if not isinstance(visit, str) or not visit or not isinstance(enc, str) or not enc:
            raise VendorRejected(
                "bahmni: document filing response had no visitUuid/encounterUuid", landed=True
            )
        return RecordDocumentResult(visit_uuid=visit, encounter_uuid=enc, url=url)
