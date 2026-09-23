#!/usr/bin/env python3
"""Live write-back proof against the harness OpenEMR. Run from the repo with
`uv run python harness/openemr/verify_write_back.py`. Exits non-zero on any
failed check. Needs setup_client.py + seed_data.py + seed_match_fixtures.py run.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.adapters.openemr import OpenEmrAdapter
from sm_common.integrations.canonical_types import AppointmentWrite
from sm_common.integrations.exceptions import WriteNotSupported

HERE = pathlib.Path(__file__).parent
BASE = "https://localhost:9300"
FHIR = f"{BASE}/apis/default/fhir"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        failures.append(name)


def db_count(pc_uuid_hex_marker: str) -> int:
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "mysql",
            "mariadb",
            "-uroot",
            "-popenemr_root",
            "openemr",
            "-N",
            "-e",
            "SELECT COUNT(*) FROM openemr_postcalendar_events WHERE pc_hometext LIKE "
            f"'%{pc_uuid_hex_marker}%';",
        ],
        cwd=HERE,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


async def main() -> int:
    client = json.loads((HERE / "client.json").read_text())
    seed = json.loads((HERE / "seed_client.json").read_text())
    jwks = json.loads((HERE / "jwks" / "jwks.json").read_text())
    sys_cfg = {
        "token_url": f"{BASE}/oauth2/default/token",
        "client_id": client["client_id"],
        "private_key_pem": (HERE / "private_key.pem").read_text(),
        "kid": jwks["keys"][0]["kid"],
        "scopes": "system/Patient.read system/Appointment.read system/Practitioner.read",
    }
    write_user = {
        "token_url": f"{BASE}/oauth2/default/token",
        "client_id": seed["client_id"],
        "client_secret": seed.get("client_secret", ""),
        "username": "admin",
        "password": "SpatiaHarness#2026",
        "scopes": "openid api:oemr api:fhir user/patient.read user/appointment.read "
        "user/appointment.write user/practitioner.read",
    }
    oe = OpenEmrAdapter(
        base_url=FHIR,
        auth_scheme="private_key_jwt",
        auth_cfg=sys_cfg,
        openemr={
            "timezone": "Asia/Kolkata",
            "pc_catid": "5",
            "pc_facility": "3",
            "pc_billing_location": "3",
            "write_user": write_user,
        },
    )
    # The harness serves a self-signed cert on localhost; product code never does this.
    oe._client = httpx.AsyncClient(verify=False, timeout=30.0)

    patients = await oe.search_patients(phone="9000000101")
    check(
        "match fixture: plain-success patient found exactly once",
        len(patients) == 1,
        f"n={len(patients)}",
    )
    ambiguous = await oe.search_patients(phone="9000000102")
    check("household phone returns both members", len(ambiguous) == 2, f"n={len(ambiguous)}")
    check("find_patient refuses the household", await oe.find_patient(phone="9000000102") is None)

    roster = await oe.fetch_doctor_roster(datetime.now(UTC).date())
    prac = roster[0].external_doctor_id
    bid = uuid.uuid4()
    start = (datetime.now(UTC) + timedelta(days=7)).replace(
        hour=4, minute=30, second=0, microsecond=0
    )
    w = AppointmentWrite(
        bid, patients[0].resource_id or "", prac, start, start + timedelta(minutes=15), "Fever"
    )

    r1 = await oe.write_back_idempotent(w)
    check("create lands", r1.created and bool(r1.hms_booking_id), str(r1))
    check(
        "HMS time equals our time (tz conversion)",
        r1.hms_start == start,
        f"{r1.hms_start} vs {start}",
    )
    fhir = await oe._client.get(
        f"{FHIR}/Appointment/{r1.hms_booking_id}", headers=await oe._headers()
    )
    check(
        "hms_booking_id is the FHIR Appointment.id",
        fhir.status_code == 200,
        f"HTTP {fhir.status_code}",
    )

    r2 = await oe.write_back_idempotent(w)
    check(
        "retry finds, does not duplicate",
        (not r2.created) and r2.hms_booking_id == r1.hms_booking_id,
    )
    check("exactly one row in OpenEMR", db_count(str(bid)) == 1, f"count={db_count(str(bid))}")

    appts, _ = await oe.list_appointments_modified_since("", (start + timedelta(days=1)).date())
    ids = {a.appointment_id for a in appts}
    check("ingest sees our write under the same id", r1.hms_booking_id in ids)

    c = await oe.cancel(r1.hms_booking_id or "", "verify")
    check("cancel", c.status == "SUCCESS", str(c))
    check("cancelled row gone", db_count(str(bid)) == 0)

    fhir_only = FhirR4Adapter(base_url=FHIR, auth_scheme="private_key_jwt", auth_cfg=dict(sys_cfg))
    fhir_only._client = httpx.AsyncClient(verify=False, timeout=30.0)
    try:
        await fhir_only.write_back_idempotent(w)
        check("plain FHIR write on OpenEMR is WriteNotSupported", False, "no exception")
    except WriteNotSupported:
        check("plain FHIR write on OpenEMR is WriteNotSupported", True)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
