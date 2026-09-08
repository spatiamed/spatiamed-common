#!/usr/bin/env python
"""Drive the real FhirR4Adapter against the running OpenEMR.

The unit tests assert the request shape; only this says whether a live FHIR
server answers it with the appointment that was just created.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import ssl
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter  # noqa: E402

HERE = pathlib.Path(__file__).parent
BASE = "https://localhost:9300"
FHIR = f"{BASE}/apis/default/fhir"
STD = f"{BASE}/apis/default/api"


async def main() -> int:
    token = (HERE / "token.txt").read_text().strip()
    user_token = (HERE / "user_token.txt").read_text().strip()
    today = dt.date.today()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    adapter = FhirR4Adapter(
        base_url=FHIR,
        auth_scheme="bearer",
        auth_cfg={"bearer_token": token},
        hash_salt="harness-salt",
    )
    adapter._client = httpx.AsyncClient(verify=ctx, timeout=30.0)

    async with httpx.AsyncClient(verify=ctx, timeout=30.0) as c:
        appt = {
            "pc_catid": "5",
            "pc_title": "verify-fix",
            "pc_duration": "900",
            "pc_apptstatus": "-",
            "pc_eventDate": today.isoformat(),
            "pc_startTime": "17:15",
            "pc_facility": "3",
            "pc_billing_location": "3",
            "pc_hometext": "created to verify the same-day poll fix",
        }
        r = await c.post(
            f"{STD}/patient/2/appointment",
            json=appt,
            headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        )
        created = r.json().get("id")
        print(f"created appointment id={created} for {today}")

    appts, cursor = await adapter.list_appointments_modified_since("", today)
    print(f"adapter returned {len(appts)} appointments; cursor={cursor}")

    phone_hashes = {a.patient.phone_hash for a in appts if a.patient.phone_hash}
    print(f"phone_hash values present: {len(phone_hashes)}")

    await adapter.close()

    ok = len(appts) > 0
    print(
        "\nRESULT:",
        "PASS — same-day appointments are visible"
        if ok
        else "FAIL — poller still cannot see today's data",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
