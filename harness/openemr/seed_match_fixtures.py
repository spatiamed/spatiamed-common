#!/usr/bin/env -S uv run --quiet --with httpx --script
"""Seed patients that exercise every branch of the kiosk matching rule.

The original seed_data.py creates one patient, and running it repeatedly left
six records sharing a phone AND a name — an ambiguity fixture, so every lookup
correctly refuses to bind and no successful match can be demonstrated.

These fixtures cover the whole decision surface, so a demo shows the safety
rules working rather than only a happy path:

  phone        in OpenEMR                        kiosk submits        expected
  ---------------------------------------------------------------------------
  9000000101   Asha Rao, F, 1991                 Asha Rao, F, 34      matched
  9000000102   Vikram Rao M + Priya Rao F        Priya Rao, F, 29     ambiguous
  9000000103   Rajesh Kumar, M, 1980             Rajesh Kumar, F, 45  ambiguous
  9000000104   Kavita . , F, 1994                Kavita, F, 32        matched
  9000000105   Sunita Devi Sharma, F, 1988       Sunita Sharma, F, 37 matched
  9000000106   Asha Sharma, F, 1991              Asha Rao, F, 34      ambiguous

Why each one is here:
  ...101  the plain success case
  ...102  a shared household phone — two people, so the HMS itself cannot say
          which is ours
  ...103  gender vetoes an otherwise perfect name match (siblings)
  ...104  a mononym: thin evidence, binds only because age AND gender agree
  ...105  an extra middle name in the HMS must still match
  ...106  same given name, different surname — the household false positive a
          first-token rule would wrongly accept

Run with the harness up:
    ./seed_match_fixtures.py
"""

from __future__ import annotations

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

# (fname, lname, sex, DOB, phone)
FIXTURES = [
    ("Asha", "Rao", "Female", "1991-04-12", "9000000101"),
    ("Vikram", "Rao", "Male", "1985-08-03", "9000000102"),
    ("Priya", "Rao", "Female", "1996-11-20", "9000000102"),
    ("Rajesh", "Kumar", "Male", "1980-02-28", "9000000103"),
    # OpenEMR refuses an empty last name AND anything under two characters
    # ("Last Name must be 2 characters or longer"), so a true mononym cannot be
    # stored at all — and many Indian patients have only one name. Repeating
    # the given name is the commonest workaround and is what real data looks
    # like. Our token set dedupes it to {kavita} against the kiosk's {kavita}:
    # one shared token on both sides, the WEAK path, which binds only because
    # age and gender also corroborate.
    #
    # CAVEAT worth knowing: a hospital that instead pads with "NA" or "XX"
    # produces {kavita, na}, which does NOT match a kiosk's {kavita} — one
    # shared token but no longer a single-token name on both sides. That is
    # the safe answer (we genuinely cannot tell), but it means a hospital's
    # placeholder convention changes how many patients auto-match.
    ("Kavita", "Kavita", "Female", "1994-06-15", "9000000104"),
    ("Sunita", "Devi Sharma", "Female", "1988-09-09", "9000000105"),
    ("Asha", "Sharma", "Female", "1991-04-12", "9000000106"),
]


def register_user_client(client: httpx.Client) -> tuple[str, str]:
    body = {
        "application_type": "private",
        "client_name": "SpatiaMed Match Fixtures",
        "grant_types": ["password", "refresh_token"],
        "redirect_uris": [f"{BASE}/unused"],
        "scope": SCOPES,
    }
    r = client.post(f"{BASE}/oauth2/default/registration", json=body, timeout=30.0)
    if r.status_code not in (200, 201):
        raise SystemExit(f"registration failed: {r.status_code} {r.text[:400]}")
    data = r.json()
    (HERE / "match_fixtures_client.json").write_text(json.dumps(data, indent=2))
    # Newly registered clients start disabled; only an admin can enable them,
    # and OpenEMR exposes no API for it.
    subprocess.run(
        [
            "docker", "compose", "exec", "-T", "mysql", "mariadb",
            "-uroot", "-popenemr_root", "openemr", "-e",
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
    with httpx.Client(verify=False, timeout=30.0) as client:
        cid, secret = register_user_client(client)
        token = user_token(client, cid, secret)
        print("user-role token obtained\n")
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        created = 0
        for fname, lname, sex, dob, phone in FIXTURES:
            body = {
                "fname": fname,
                "lname": lname,
                "sex": sex,
                "DOB": dob,
                "phone_cell": phone,
            }
            r = client.post(f"{STD}/patient", json=body, headers=h)
            label = f"{fname} {lname}".strip()
            if r.status_code >= 400:
                print(f"  FAILED {label:24} {phone}  -> {r.status_code} {r.text[:120]}")
                continue
            data = r.json().get("data", {})
            print(f"  seeded {label:24} {phone}  pid={data.get('pid')}")
            created += 1

        print(f"\n{created} of {len(FIXTURES)} fixture patients created.")
        if created != len(FIXTURES):
            return 1

        print(
            "\nNext: run verify_match_fixtures.py to confirm each phone resolves\n"
            "to the expected outcome against the live server."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
