#!/usr/bin/env -S uv run --quiet --with httpx --script
"""Record OpenEMR Standard-API responses the OpenEmrAdapter parses.

Output lands in tests/integrations/fixtures/openemr/ and is committed: the
adapter's unit tests replay these, so they test OpenEMR's real shapes. Re-run
when the harness's OpenEMR version changes.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import httpx

HERE = pathlib.Path(__file__).parent
OUT = HERE.parent.parent / "tests" / "integrations" / "fixtures" / "openemr"
BASE = "https://localhost:9300"
STD = f"{BASE}/apis/default/api"


def save(name: str, resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(
        json.dumps({"status": resp.status_code, "body": body}, indent=2, default=str)
    )
    print(f"{name}: HTTP {resp.status_code}")
    return body if isinstance(body, dict) else {}


def main() -> int:
    token = (HERE / "user_token.txt").read_text().strip()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(verify=False, timeout=30.0) as c:
        patients = c.get(f"{STD}/patient", headers=h).json()["data"]
        p = patients[-1]
        pid, puuid = p["pid"], p["uuid"]
        save("patient_get", c.get(f"{STD}/patient/{puuid}", headers=h))
        prac = c.get(f"{STD}/practitioner", headers=h).json()["data"][0]
        save("practitioner_get", c.get(f"{STD}/practitioner/{prac['uuid']}", headers=h))
        day = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        appt = {
            "pc_catid": "5",
            "pc_title": "Fixture capture",
            "pc_duration": "900",
            "pc_hometext": "Fever [spatiamed:00000000-0000-0000-0000-0000000000f1]",
            "pc_apptstatus": "-",
            "pc_eventDate": day,
            "pc_startTime": "10:00",
            "pc_facility": "3",
            "pc_billing_location": "3",
            "pc_aid": str(prac["id"]),
        }
        post = c.post(f"{STD}/patient/{pid}/appointment", json=appt, headers=h)
        save("appointment_post", post)
        bad = {k: v for k, v in appt.items() if k != "pc_hometext"}
        save(
            "appointment_post_missing_hometext",
            c.post(f"{STD}/patient/{pid}/appointment", json=bad, headers=h),
        )
        list_resp = c.get(f"{STD}/patient/{pid}/appointment", headers=h)
        save("patient_appointments_list", list_resp)
        listing = list_resp.json()
        rows = listing if isinstance(listing, list) else (listing.get("data") or [])
        eid = max(int(r["pc_eid"]) for r in rows)
        save("appointment_get", c.get(f"{STD}/appointment/{eid}", headers=h))
        save("appointment_delete", c.delete(f"{STD}/patient/{pid}/appointment/{eid}", headers=h))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
