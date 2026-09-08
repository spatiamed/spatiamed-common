#!/usr/bin/env -S uv run --quiet --with httpx --script
"""Seed one patient + one appointment dated today, via OpenEMR's Standard API.

Writes need a *user-role* token: OpenEMR's routes file states "the api route is
only for users role", so the system-scoped client_credentials token used for the
FHIR reads cannot create anything. This registers a separate password-grant
client for the write leg.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys

import httpx

HERE = pathlib.Path(__file__).parent
BASE = "https://localhost:9300"
STD = f"{BASE}/apis/default/api"

OE_USER = "admin"
OE_PASS = "SpatiaHarness#2026"

SCOPES = (
    "openid offline_access api:oemr api:fhir "
    "user/patient.write user/patient.read "
    "user/appointment.write user/appointment.read"
)


def register_user_client(client: httpx.Client) -> str:
    body = {
        "application_type": "private",
        "client_name": "SpatiaMed Harness Seeder",
        "grant_types": ["password", "refresh_token"],
        "redirect_uris": [f"{BASE}/unused"],
        "scope": SCOPES,
    }
    r = client.post(f"{BASE}/oauth2/default/registration", json=body, timeout=30.0)
    if r.status_code not in (200, 201):
        raise SystemExit(f"registration failed: {r.status_code} {r.text[:400]}")
    data = r.json()
    (HERE / "seed_client.json").write_text(json.dumps(data, indent=2))
    subprocess.run(
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
            "-e",
            f"UPDATE oauth_clients SET is_enabled = 1 WHERE client_id = '{data['client_id']}';",
        ],
        cwd=HERE,
        check=True,
    )
    return data["client_id"], data.get("client_secret", "")


def user_token(client: httpx.Client, client_id: str, client_secret: str) -> str:
    r = client.post(
        f"{BASE}/oauth2/default/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": SCOPES,
            "user_role": "users",
            "username": OE_USER,
            "password": OE_PASS,
        },
        timeout=30.0,
    )
    if r.status_code != 200:
        raise SystemExit(f"password grant failed: {r.status_code} {r.text[:400]}")
    return r.json()["access_token"]


def main() -> int:
    today = dt.date.today()
    with httpx.Client(verify=False, timeout=30.0) as client:
        cid, secret = register_user_client(client)
        print(f"seed client: {cid}")
        token = user_token(client, cid, secret)
        print("user-role token obtained")
        (HERE / "user_token.txt").write_text(token)
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        patient = {
            "fname": "Harness",
            "lname": "Patient",
            "sex": "Male",
            "DOB": "1990-01-01",
            "phone_cell": "9000000001",
        }
        r = client.post(f"{STD}/patient", json=patient, headers=h)
        print(f"POST /api/patient -> {r.status_code}")
        if r.status_code >= 400:
            print(r.text[:400])
            return 1
        pdata = r.json().get("data", {})
        pid = pdata.get("pid") or pdata.get("id")
        puuid = pdata.get("uuid")
        print(f"  patient pid={pid} uuid={puuid}")

        appt = {
            "pc_catid": "5",
            "pc_title": "Harness sync test",
            "pc_duration": "900",
            "pc_hometext": "created by the OpenEMR harness",
            "pc_apptstatus": "-",
            "pc_eventDate": today.isoformat(),
            "pc_startTime": "10:00",
            "pc_facility": "3",
            "pc_billing_location": "3",
        }
        r = client.post(f"{STD}/patient/{pid}/appointment", json=appt, headers=h)
        print(f"POST /api/patient/{pid}/appointment -> {r.status_code}")
        print(f"  {r.text[:300]}")
        return 0 if r.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
