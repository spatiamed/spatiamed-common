#!/usr/bin/env -S uv run --quiet --with httpx --script
"""Measure the four things source reading could only guess at.

1. Does a system-scoped token actually get refused by the Standard API?
2. Does OpenEMR paginate a FHIR bundle, and does it honour _count?
3. What does a real OpenEMR Patient look like to _patient_to_canonical?
4. How long after a create does the appointment become visible to the poller?
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time

import httpx

HERE = pathlib.Path(__file__).parent
BASE = "https://localhost:9300"
FHIR = f"{BASE}/apis/default/fhir"
STD = f"{BASE}/apis/default/api"


def main() -> int:
    system_token = (HERE / "token.txt").read_text().strip()
    user_token = sys.argv[1]
    sysh = {"Authorization": f"Bearer {system_token}", "Content-Type": "application/json"}
    useh = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}

    with httpx.Client(verify=False, timeout=30.0) as c:
        print("== 1. system token vs Standard API write route ==")
        appt = {
            "pc_catid": "5", "pc_title": "role probe", "pc_duration": "900",
            "pc_apptstatus": "-", "pc_eventDate": dt.date.today().isoformat(),
            "pc_startTime": "11:00", "pc_facility": "3", "pc_billing_location": "3",
            # Required: omitting it yields HTTP 200 and no record (finding 7.1).
            "pc_hometext": "harness probe",
        }
        r = c.post(f"{STD}/patient/2/appointment", json=appt, headers=sysh)
        print(f"  POST /api/patient/2/appointment with SYSTEM token -> {r.status_code}")
        print(f"    {r.text[:200]}")

        print("\n== 3. what a real Patient looks like ==")
        r = c.get(f"{FHIR}/Patient", headers=sysh)
        entries = r.json().get("entry", [])
        if entries:
            res = entries[0]["resource"]
            print(f"  identifier: {json.dumps(res.get('identifier'))}")
            print(f"  telecom:    {json.dumps(res.get('telecom'))}")
            print(f"  id:         {res.get('id')}")
        else:
            print("  no patients returned")

        print("\n== 2. pagination / _count ==")
        # Seed enough appointments to exceed a small _count.
        made = 0
        for i in range(14):
            a = dict(appt, pc_title=f"page probe {i}", pc_startTime=f"{9 + i % 8:02d}:{(i * 5) % 60:02d}")
            rr = c.post(f"{STD}/patient/2/appointment", json=a, headers=useh)
            if rr.status_code < 400 and rr.json().get("id"):
                made += 1
        print(f"  seeded {made} extra appointments")
        r = c.get(f"{FHIR}/Appointment", params={"_count": "5"}, headers=sysh)
        b = r.json()
        n = len(b.get("entry", []))
        links = [(l.get("relation"), l.get("url", "")[:60]) for l in b.get("link", [])]
        print(f"  GET /Appointment?_count=5 -> {r.status_code}, {n} entries, total={b.get('total')}")
        print(f"  bundle links: {links}")
        print(f"  VERDICT: _count {'HONOURED' if n == 5 else 'IGNORED'}; "
              f"next link {'PRESENT' if any(l[0] == 'next' for l in links) else 'ABSENT'}")

        print("\n== 4. create -> visible latency (inbound floor) ==")
        a = dict(appt, pc_title="latency probe", pc_startTime="15:30")
        t_create = time.monotonic()
        rr = c.post(f"{STD}/patient/2/appointment", json=a, headers=useh)
        made_id = rr.json().get("id") if rr.status_code < 400 else None
        if not made_id:
            print(f"  create FAILED: {rr.status_code} {rr.text[:160]}")
        print(f"  created appointment id={made_id}")
        seen = None
        for _ in range(30):
            q = c.get(f"{FHIR}/Appointment",
                      params={"_lastUpdated": "gt1970-01-01T00:00:00+00:00"}, headers=sysh)
            titles = [e["resource"].get("description", "") for e in q.json().get("entry", [])]
            if any("latency probe" in (t or "") for t in titles):
                seen = time.monotonic() - t_create
                break
            time.sleep(1)
        if seen is not None:
            print(f"  visible to the corrected poller query after {seen:.2f}s")
        else:
            print("  NOT visible within 30s")

        # What timestamp granularity does _lastUpdated carry?
        q = c.get(f"{FHIR}/Appointment", params={"_lastUpdated": "gt1970-01-01T00:00:00+00:00"}, headers=sysh)
        lus = {e["resource"].get("meta", {}).get("lastUpdated") for e in q.json().get("entry", [])}
        print(f"  distinct meta.lastUpdated values seen: {sorted(x for x in lus if x)[:4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
