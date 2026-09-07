#!/usr/bin/env -S uv run --quiet --with httpx --with cryptography --with pyjwt --script
"""Probe OpenEMR with the exact requests FhirR4Adapter sends.

The point is not to check that OpenEMR works — it is to see what a real server
does with our adapter's requests, which respx cannot show. Every query below is
copied from sm_common/integrations/adapters/fhir_r4.py rather than idealised.
"""

from __future__ import annotations

import datetime as dt
import json
import sys

import httpx

BASE = "https://localhost:9300"
FHIR = f"{BASE}/apis/default/fhir"
STD = f"{BASE}/apis/default/api"

results: list[tuple[str, str, str]] = []


def record(method: str, detail: str, verdict: str) -> None:
    results.append((method, detail, verdict))
    print(f"  [{verdict:7}] {method}: {detail}")


def probe_reads(client: httpx.Client, token: str) -> None:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    today = dt.date.today()

    # health_check -> GET /metadata
    r = client.get(f"{FHIR}/metadata", headers=h)
    record("health_check", f"GET /metadata -> {r.status_code}", "PASS" if r.status_code == 200 else "FAIL")

    # find_patient -> GET /Patient?identifier=
    r = client.get(f"{FHIR}/Patient", params={"identifier": "test-mrn"}, headers=h)
    record("find_patient", f"GET /Patient?identifier= -> {r.status_code}", "PASS" if r.status_code == 200 else "FAIL")

    # fetch_doctor_roster -> GET /Practitioner
    r = client.get(f"{FHIR}/Practitioner", headers=h)
    record("fetch_doctor_roster", f"GET /Practitioner -> {r.status_code}", "PASS" if r.status_code == 200 else "FAIL")

    # list_appointments_modified_since -> the adapter's literal query.
    # until_date is the tenant's clinic day, sent as an upper bound on the
    # MODIFICATION time. If that excludes today, same-day sync is impossible.
    params = {
        "_lastUpdated": ["gt1970-01-01T00:00:00+00:00", f"lt{today.isoformat()}"],
        "_count": "100",
        "_sort": "_lastUpdated",
    }
    r = client.get(f"{FHIR}/Appointment", params=params, headers=h)
    body = r.text[:300]
    if r.status_code != 200:
        record("list_appointments (adapter query)", f"-> {r.status_code}: {body}", "FAIL")
    else:
        n = len(r.json().get("entry", []))
        record("list_appointments (adapter query)", f"-> 200, {n} entries (lt{today})", "PASS")

    # The same query WITHOUT the lt upper bound, for comparison.
    params_no_lt = {"_lastUpdated": ["gt1970-01-01T00:00:00+00:00"], "_count": "100"}
    r2 = client.get(f"{FHIR}/Appointment", params=params_no_lt, headers=h)
    if r2.status_code == 200:
        n2 = len(r2.json().get("entry", []))
        record("list_appointments (no lt bound)", f"-> 200, {n2} entries", "INFO")

    # Does OpenEMR reject the params we send, or silently ignore them?
    r3 = client.get(f"{FHIR}/Appointment", params={"_sort": "_lastUpdated"}, headers=h)
    record("_sort support", f"GET /Appointment?_sort=_lastUpdated -> {r3.status_code}", "INFO")


def probe_writes(client: httpx.Client, token: str) -> None:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # write_back_idempotent -> PUT /Appointment/{id}
    r = client.put(f"{FHIR}/Appointment/probe-uuid", json={"resourceType": "Appointment", "status": "booked"}, headers=h)
    record("write_back_idempotent", f"PUT /Appointment/{{id}} -> {r.status_code}", "FAIL" if r.status_code >= 400 else "PASS")

    # cancel -> PUT /Appointment/{id}
    record("cancel", "same route as write_back_idempotent", "FAIL" if r.status_code >= 400 else "PASS")

    # push_visit_event -> POST /Encounter
    r = client.post(f"{FHIR}/Encounter", json={"resourceType": "Encounter", "status": "arrived"}, headers=h)
    record("push_visit_event", f"POST /Encounter -> {r.status_code}", "FAIL" if r.status_code >= 400 else "PASS")


def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else ""
    if not token:
        print("usage: probe_conformance.py <access_token>")
        return 2

    with httpx.Client(verify=False, timeout=30.0) as client:
        print("\nREADS (FhirR4Adapter inbound):")
        probe_reads(client, token)
        print("\nWRITES (FhirR4Adapter outbound):")
        probe_writes(client, token)

    print("\n" + "=" * 60)
    for method, detail, verdict in results:
        print(f"{verdict:7} | {method:38} | {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
