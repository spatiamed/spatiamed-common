"""Bahmni patient record client: search, read, marker-idempotent create, update."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

from sm_common.identity.dob import age_on
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_parse import birth_date as parse_birth_date
from sm_common.integrations.adapters.bahmni_parse import flag, gender
from sm_common.integrations.exceptions import (
    ConfigError,
    ConflictError,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.record_types import (
    RecordAddress,
    RecordPatient,
    RecordPatientInput,
    RecordPatientPatch,
)

_REST = "/openmrs/ws/rest/v1"
_PROFILE_WRITE = f"{_REST}/bahmnicore/patientprofile"
_MARKER_PREFIX = "sm:"


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
