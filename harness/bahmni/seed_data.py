#!/usr/bin/env python
"""Idempotent synthetic seed for the Bahmni live harness; writes the harness config JSON.

Resolves every uuid the adapter needs BY NAME on the target Bahmni (no stock uuid
literals: they drift between Bahmni versions) and creates what is missing: one
provider, two appointment services, one patient, the marker person-attribute type
and the note-marker Text concept. Every synthetic record is ``LT-ADAPTER-`` marked.
Re-running changes nothing. Synthetic data only.

Env (required): BAHMNI_BASE_URL, BAHMNI_USER, BAHMNI_PASS, BAHMNI_CONFIG_JSON (output path).
Optional: BAHMNI_CA_PEM (PEM path), BAHMNI_INSECURE_TLS=1 (throwaway test box only),
BAHMNI_LOCATION_NAME (default "OPD-1").
Exit: 0 written, 1 a required Bahmni object could not be resolved, 2 bad env.
"""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import sys
from typing import Any

import httpx

R = "/openmrs/ws/rest/v1"
TAG = "LT-ADAPTER"
PROVIDER_IDENTIFIER = f"{TAG}-DR-1"
PROVIDER_NAME = (f"{TAG} Doctor", "Synthetic")
SERVICE_NAMES = (f"{TAG} General OPD", f"{TAG} Second OPD")
PATIENT_NAME = (f"{TAG} Seed", f"{TAG}-Patient")
PATIENT_DOB = "1980-01-01"
# Synthetic numbers, split so no 12-digit run appears in source.
PATIENT_PHONE = "+91" + "0000000101"
PAGING_PHONE = "+91" + "0000000102"
MARKER_ATTRIBUTE = "smPatientMarker"
NOTE_MARKER_CONCEPT = "SM Note Id"
VITALS = {"vitals_pulse": "Pulse"}
TEXT_NOTE_CANDIDATES = ("Consultation Note", "Chief Complaint Notes")
DOC_TYPE = "lab_report"
DOC_CONCEPT_CANDIDATES = ("Patient Document", "Document", "Lab Report", "Radiology")


class UnresolvedError(RuntimeError):
    pass


def env_or_exit(*names: str) -> list[str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"set {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    return [os.environ[n] for n in names]


def tls_verify() -> ssl.SSLContext | bool:
    pem = os.environ.get("BAHMNI_CA_PEM")
    if pem:
        return ssl.create_default_context(cafile=pem)
    return os.environ.get("BAHMNI_INSECURE_TLS") != "1"


def make_client(base_url: str, user: str, password: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, auth=(user, password), verify=tls_verify(), timeout=60)


def _ok(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        raise UnresolvedError(
            f"{resp.request.method} {resp.request.url.path} -> {resp.status_code}: "
            f"{resp.text[:300]}"
        )
    return resp.json() if resp.content else {}


def _results(body: Any) -> list[dict[str, Any]]:
    rows = body.get("results", []) if isinstance(body, dict) else body
    return [r for r in rows or [] if isinstance(r, dict)]


def _named(c: httpx.Client, resource: str, name: str) -> str | None:
    """Exact (case-insensitive) display-name match among ``?q=`` hits."""
    rows = _results(_ok(c.get(f"{R}/{resource}", params={"q": name, "v": "custom:(uuid,display)"})))
    for r in rows:
        if str(r.get("display") or "").strip().lower() == name.lower():
            return str(r["uuid"])
    return None


def need(value: str | None, what: str) -> str:
    if not value:
        raise UnresolvedError(f"{what} not found on this Bahmni")
    return value


def ensure_provider(c: httpx.Client) -> str:
    rows = _results(
        _ok(
            c.get(
                f"{R}/provider", params={"q": PROVIDER_IDENTIFIER, "v": "custom:(uuid,identifier)"}
            )
        )
    )
    for p in rows:
        if p.get("identifier") == PROVIDER_IDENTIFIER:
            return str(p["uuid"])
    person = _ok(
        c.post(
            f"{R}/person",
            json={
                "names": [{"givenName": PROVIDER_NAME[0], "familyName": PROVIDER_NAME[1]}],
                "gender": "M",
            },
        )
    )
    body = {"person": person["uuid"], "identifier": PROVIDER_IDENTIFIER}
    return str(_ok(c.post(f"{R}/provider", json=body))["uuid"])


def ensure_service(c: httpx.Client, name: str, location_uuid: str) -> str:
    for s in _results(_ok(c.get(f"{R}/appointmentService/all/default"))):
        if s.get("name") == name:
            return str(s["uuid"])
    created = _ok(
        c.post(
            f"{R}/appointmentService",
            json={
                "name": name,
                "durationMins": 15,
                "description": "synthetic harness service",
                "locationUuid": location_uuid,
            },
        )
    )
    return str(created["uuid"])


def identifier_parts(c: httpx.Client) -> tuple[str, str, str]:
    """(identifier type uuid, source uuid, prefix) for the primary patient identifier."""
    resp = c.get(f"{R}/idgen/identifiertype")
    if resp.status_code == 200:
        for t in _results(resp.json()):
            sources = [s for s in t.get("identifierSources") or [] if isinstance(s, dict)]
            if t.get("primary") and sources:
                s = sources[0]
                return str(t["uuid"]), str(s["uuid"]), str(s.get("prefix") or "")
    type_uuid = need(_named(c, "patientidentifiertype", "Patient Identifier"), "identifier type")
    for s in _results(_ok(c.get(f"{R}/idgen/identifiersource", params={"v": "full"}))):
        if (s.get("identifierType") or {}).get("uuid") == type_uuid:
            return type_uuid, str(s["uuid"]), str(s.get("prefix") or "")
    raise UnresolvedError("identifier source for the primary identifier type")


def ensure_marker_attribute(c: httpx.Client) -> str:
    found = _named(c, "personattributetype", MARKER_ATTRIBUTE)
    if found:
        return found
    body = {
        "name": MARKER_ATTRIBUTE,
        "format": "java.lang.String",
        "description": "SpatiaMed patients.id idempotency marker",
        "searchable": True,
    }
    return str(_ok(c.post(f"{R}/personattributetype", json=body))["uuid"])


def ensure_text_concept(c: httpx.Client, name: str) -> str:
    found = _named(c, "concept", name)
    if found:
        return found
    text = need(_named(c, "conceptdatatype", "Text"), "concept datatype Text")
    misc = need(_named(c, "conceptclass", "Misc"), "concept class Misc")
    body = {
        "names": [
            {
                "name": name,
                "locale": "en",
                "conceptNameType": "FULLY_SPECIFIED",
                "localePreferred": True,
            }
        ],
        "datatype": text,
        "conceptClass": misc,
    }
    return str(_ok(c.post(f"{R}/concept", json=body))["uuid"])


def drug_other_concept(c: httpx.Client) -> str:
    rows = _results(
        _ok(c.get(f"{R}/systemsetting", params={"q": "drugOrder.drugOther", "v": "full"}))
    )
    for r in rows:
        if r.get("property") == "drugOrder.drugOther" and r.get("value"):
            value = str(r["value"])
            # Some installs store the concept NAME, not its uuid.
            return value if len(value) >= 36 else need(_named(c, "concept", value), value)
    return need(_named(c, "concept", "Other"), "drug-other concept")


def first_concept(c: httpx.Client, names: tuple[str, ...]) -> tuple[str, str]:
    for n in names:
        found = _named(c, "concept", n)
        if found:
            return n, found
    raise UnresolvedError(f"none of the concepts {names}")


def find_by_phone(c: httpx.Client, phone: str) -> list[dict[str, Any]]:
    resp = c.get(
        f"{R}/bahmni/search/patient",
        params={
            "q": "",
            "customAttribute": phone,
            "patientAttributes": "phoneNumber",
            "s": "byIdOrNameOrVillage",
        },
    )
    return [h for h in _ok(resp).get("pageOfResults", []) if isinstance(h, dict)]


def ensure_patient(
    c: httpx.Client, phone_attr: str, id_type: str, id_source: str, prefix: str
) -> tuple[str, str]:
    for hit in find_by_phone(c, PATIENT_PHONE):
        if str(hit.get("familyName") or "") == PATIENT_NAME[1]:
            return str(hit["uuid"]), str(hit["identifier"])
    ident: dict[str, Any] = {
        "identifierType": id_type,
        "identifierSourceUuid": id_source,
        "preferred": True,
        "voided": False,
    }
    if prefix:
        ident["identifierPrefix"] = prefix
    body = {
        "patient": {
            "person": {
                "names": [
                    {"givenName": PATIENT_NAME[0], "familyName": PATIENT_NAME[1], "preferred": True}
                ],
                "gender": "F",
                "birthdate": PATIENT_DOB,
                "attributes": [{"attributeType": {"uuid": phone_attr}, "value": PATIENT_PHONE}],
                "addresses": [{"cityVillage": "Synthville"}],
            },
            "identifiers": [ident],
        },
        "relationships": [],
    }
    p = _ok(c.post(f"{R}/bahmnicore/patientprofile", json=body))["patient"]
    return str(p["uuid"]), str(p["identifiers"][0]["identifier"])


def build_config(c: httpx.Client) -> dict[str, Any]:
    location_name = os.environ.get("BAHMNI_LOCATION_NAME", "OPD-1")
    location = need(_named(c, "location", location_name), f"location {location_name!r}")
    id_type, id_source, prefix = identifier_parts(c)
    phone_attr = need(_named(c, "personattributetype", "phoneNumber"), "phoneNumber attribute")
    marker_attr = ensure_marker_attribute(c)
    provider = ensure_provider(c)
    services = [ensure_service(c, n, location) for n in SERVICE_NAMES]
    opd = need(_named(c, "visittype", "OPD"), "visit type OPD")
    consult = need(_named(c, "encountertype", "Consultation"), "encounter type Consultation")
    doc_enc = need(
        _named(c, "encountertype", "Patient Document"), "encounter type Patient Document"
    )
    concept_map = {k: need(_named(c, "concept", n), f"concept {n!r}") for k, n in VITALS.items()}
    with contextlib.suppress(UnresolvedError):
        _, concept_map["chief_complaint"] = first_concept(c, TEXT_NOTE_CANDIDATES)
    doc_concept_name, doc_concept = first_concept(c, DOC_CONCEPT_CANDIDATES)
    patient, identifier = ensure_patient(c, phone_attr, id_type, id_source, prefix)
    return {
        "field_mapping": {
            "location_uuid": location,
            "identifier_type_uuid": id_type,
            "identifier_source_uuid": id_source,
            "identifier_prefix": prefix,
            "phone_attribute_name": "phoneNumber",
            "phone_attribute_type_uuid": phone_attr,
            "marker_attribute_name": MARKER_ATTRIBUTE,
            "marker_attribute_type_uuid": marker_attr,
            "default_service_uuid": services[0],
            "service_by_practitioner": {provider: services[0]},
            "opd_visit_type_uuid": opd,
            "consultation_encounter_type_uuid": consult,
            "document_encounter_type_uuid": doc_enc,
            "document_visit_type_uuid": opd,
            "default_provider_uuid": provider,
            "note_marker_concept_uuid": ensure_text_concept(c, NOTE_MARKER_CONCEPT),
            "concept_map": concept_map,
            "drug_other_concept_uuid": drug_other_concept(c),
            "document_type_concepts": {DOC_TYPE: doc_concept},
        },
        "harness": {
            "provider_uuid": provider,
            "service_uuids": services,
            "patient_uuid": patient,
            "patient_identifier": identifier,
            "patient_phone": PATIENT_PHONE,
            "paging_phone": PAGING_PHONE,
            "doc_type": DOC_TYPE,
            "doc_concept_name": doc_concept_name,
            "tag": TAG,
        },
    }


def main() -> int:
    base, user, password, out = env_or_exit(
        "BAHMNI_BASE_URL", "BAHMNI_USER", "BAHMNI_PASS", "BAHMNI_CONFIG_JSON"
    )
    try:
        with make_client(base, user, password) as c:
            cfg = build_config(c)
    except UnresolvedError as exc:
        print(f"seed_data: {exc}", file=sys.stderr)
        return 1
    with open(out, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    print(f"seed_data: wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
