#!/usr/bin/env -S uv run --quiet --with httpx --with cryptography --with pyjwt --script
"""Can a SYSTEM-scoped token write to OpenEMR's FHIR API?

This is the question that decides the whole outbound story. OpenEMR's Standard
API refuses system tokens outright ("the api route is only for users role"),
which is why appointment write-back needs a user account. But the FHIR API is a
different surface, and its CapabilityStatement advertises `create` on
DocumentReference and Patient.

If system scopes CAN write, then backend credentials a hospital issues us are
enough for the prescription-document path and for Stage-2 patient creation.
If they cannot, every write needs a named human user account, which is a
materially bigger ask at onboarding.

Run with the harness up and setup_client.py already executed (reuses its keypair).
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import subprocess
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from setup_client import JWKS_URI, ensure_keypair  # noqa: E402

from sm_common.integrations.auth import build_auth_headers  # noqa: E402

HERE = pathlib.Path(__file__).parent
BASE = "https://localhost:9300"
FHIR = f"{BASE}/apis/default/fhir"
TOKEN_URL = f"{BASE}/oauth2/default/token"

WRITE_SCOPES = " ".join(
    [
        "system/Patient.read",
        "system/Patient.write",
        "system/DocumentReference.read",
        "system/DocumentReference.write",
    ]
)

# The smallest structurally valid PDF, so the attachment is a real document
# rather than arbitrary bytes a server might reject on sniffing.
TINY_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def register(client: httpx.Client) -> dict:
    body = {
        "application_type": "private",
        "client_name": "SpatiaMed Write-Scope Probe",
        "grant_types": ["client_credentials"],
        "token_endpoint_auth_method": "private_key_jwt",
        "redirect_uris": ["https://localhost:9300/unused"],
        "jwks_uri": JWKS_URI,
        "scope": WRITE_SCOPES,
        "contacts": ["harness@spatiamed.test"],
    }
    r = client.post(f"{BASE}/oauth2/default/registration", json=body, timeout=30.0)
    if r.status_code not in (200, 201):
        raise SystemExit(f"registration failed: {r.status_code} {r.text[:400]}")
    data = r.json()
    (HERE / "write_probe_client.json").write_text(json.dumps(data, indent=2))
    return data


def enable(client_id: str) -> None:
    subprocess.run(
        [
            "docker", "compose", "exec", "-T", "mysql", "mariadb",
            "-uroot", "-popenemr_root", "openemr", "-e",
            f"UPDATE oauth_clients SET is_enabled = 1 WHERE client_id = '{client_id}';",
        ],
        cwd=HERE,
        check=True,
    )


async def main() -> int:
    pem, kid = ensure_keypair()
    with httpx.Client(verify=False, timeout=30.0) as c:
        reg = register(c)
        enable(reg["client_id"])
        print(f"registered write-scope client {reg['client_id'][:20]}…\n")

    async with httpx.AsyncClient(verify=False, timeout=30.0) as ac:
        try:
            headers = await build_auth_headers(
                ac,
                "private_key_jwt",
                {
                    "token_url": TOKEN_URL,
                    "client_id": reg["client_id"],
                    "private_key_pem": pem,
                    "kid": kid,
                    "scopes": WRITE_SCOPES,
                },
            )
        except Exception as exc:
            print(f"TOKEN REQUEST FAILED: {type(exc).__name__}: {exc}")
            print("\n=> OpenEMR refused to ISSUE a system token carrying .write scopes.")
            return 1

        granted = "(token issued)"
        print(f"token obtained {granted}\n")
        h = {**headers, "Content-Type": "application/fhir+json"}

        # 1. DocumentReference — the prescription-attachment path
        doc = {
            "resourceType": "DocumentReference",
            "status": "current",
            "type": {
                "coding": [
                    {"system": "http://loinc.org", "code": "11488-4",
                     "display": "Consult note"}
                ]
            },
            "subject": {"reference": "Patient/7"},
            "content": [
                {
                    "attachment": {
                        "contentType": "application/pdf",
                        "data": base64.b64encode(TINY_PDF).decode(),
                        "title": "spatiamed-probe.pdf",
                    }
                }
            ],
        }
        r = await ac.post(f"{FHIR}/DocumentReference", json=doc, headers=h)
        print(f"POST /DocumentReference -> {r.status_code}")
        print(f"  {r.text[:300]}\n")
        doc_ok = r.status_code in (200, 201)

        # 2. Patient — the Stage-2 creation path
        pat = {
            "resourceType": "Patient",
            "name": [{"family": "Probe", "given": ["System"]}],
            "gender": "female",
            "birthDate": "1990-01-01",
            "telecom": [{"system": "phone", "value": "9000000999"}],
        }
        r = await ac.post(f"{FHIR}/Patient", json=pat, headers=h)
        print(f"POST /Patient -> {r.status_code}")
        print(f"  {r.text[:300]}\n")
        pat_ok = r.status_code in (200, 201)

    print("=" * 62)
    print(f"DocumentReference create with a SYSTEM token: {'YES' if doc_ok else 'NO'}")
    print(f"Patient create with a SYSTEM token:           {'YES' if pat_ok else 'NO'}")
    print("=" * 62)
    return 0 if (doc_ok or pat_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
