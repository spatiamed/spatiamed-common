#!/usr/bin/env -S uv run --quiet --with cryptography --with pyjwt --with httpx --script
"""Register a SMART Backend Services client on the harness OpenEMR instance.

OpenEMR only grants ``system/*`` scopes to a client_credentials request carrying
an RS384 ``private_key_jwt`` assertion, so this mints a keypair, publishes the
public half through the ``jwks`` container, registers the client, enables it,
and proves the whole chain by fetching a token.

The token is fetched through ``sm_common.integrations.auth.build_auth_headers``
— the same code path the adapters use — so this exercises the production
implementation against a real server rather than a second copy of the logic.
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import subprocess
import sys
import time
import uuid

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sm_common.integrations.auth import build_auth_headers  # noqa: E402

HERE = pathlib.Path(__file__).parent
JWKS_DIR = HERE / "jwks"
PRIVATE_KEY = HERE / "private_key.pem"
CLIENT_FILE = HERE / "client.json"

BASE = "https://localhost:9300"
REGISTRATION_URL = f"{BASE}/oauth2/default/registration"
TOKEN_URL = f"{BASE}/oauth2/default/token"
JWKS_URI = "http://jwks/jwks.json"  # resolved inside the compose network

SCOPES = " ".join(
    [
        "system/Patient.read",
        "system/Appointment.read",
        "system/Practitioner.read",
        "system/Encounter.read",
    ]
)


def _b64u(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def ensure_keypair() -> tuple[str, str]:
    """Return (private_key_pem, kid), generating and publishing the JWKS once."""
    JWKS_DIR.mkdir(exist_ok=True)
    if PRIVATE_KEY.exists() and (JWKS_DIR / "jwks.json").exists():
        kid = json.loads((JWKS_DIR / "jwks.json").read_text())["keys"][0]["kid"]
        return PRIVATE_KEY.read_text(), kid

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    PRIVATE_KEY.write_text(pem)
    PRIVATE_KEY.chmod(0o600)

    numbers = key.public_key().public_numbers()
    kid = uuid.uuid4().hex
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS384",
                "n": _b64u(numbers.n),
                "e": _b64u(numbers.e),
            }
        ]
    }
    (JWKS_DIR / "jwks.json").write_text(json.dumps(jwks, indent=2))
    return pem, kid


def wait_for_openemr(client: httpx.Client, timeout: float = 900.0) -> None:
    """Block until the FHIR capability statement answers.

    The first boot runs OpenEMR's installer, which takes minutes.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            resp = client.get(f"{BASE}/apis/default/fhir/metadata", timeout=10.0)
            if resp.status_code == 200:
                print(f"OpenEMR up ({int(timeout - (deadline - time.monotonic()))}s)")
                return
            last = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)[:80]
        print(f"  waiting for OpenEMR… {last}")
        time.sleep(10)
    raise SystemExit(f"OpenEMR never became ready: {last}")


def register(client: httpx.Client) -> dict:
    body = {
        "application_type": "private",
        "client_name": "SpatiaMed HMS Harness",
        "grant_types": ["client_credentials"],
        # Required by OpenEMR's registration validator even for a grant that
        # never redirects.
        "redirect_uris": ["https://localhost:9300/unused"],
        "jwks_uri": JWKS_URI,
        "scope": SCOPES,
    }
    resp = client.post(REGISTRATION_URL, json=body, timeout=30.0)
    print(f"registration → HTTP {resp.status_code}")
    if resp.status_code not in (200, 201):
        raise SystemExit(f"registration failed: {resp.text[:500]}")
    data = resp.json()
    CLIENT_FILE.write_text(json.dumps(data, indent=2))
    return data


def enable_client(client_id: str) -> None:
    """Flip the client to enabled + trusted.

    A freshly registered OpenEMR client is inert until an admin enables it in
    Admin ▸ System ▸ API Clients; the harness must not need a human at a browser.
    """
    sql = f"UPDATE oauth_clients SET is_enabled = 1 WHERE client_id = '{client_id}';"
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
            sql,
        ],
        cwd=HERE,
        check=True,
    )
    print("client enabled")


async def fetch_token(client: httpx.AsyncClient, client_id: str, pem: str, kid: str) -> str:
    """Fetch a system-scoped token through the production auth path."""
    headers = await build_auth_headers(
        client,
        "private_key_jwt",
        {
            "token_url": TOKEN_URL,
            "client_id": client_id,
            "private_key_pem": pem,
            "kid": kid,
            "scopes": SCOPES,
        },
    )
    return headers["Authorization"].removeprefix("Bearer ")


async def main() -> int:
    pem, kid = ensure_keypair()
    # The harness serves a self-signed cert; this talks to localhost only.
    with httpx.Client(verify=False) as client:
        wait_for_openemr(client)
        registration = register(client)
        client_id = registration["client_id"]
        enable_client(client_id)
    async with httpx.AsyncClient(verify=False, timeout=30.0) as aclient:
        token = await fetch_token(aclient, client_id, pem, kid)
    (HERE / "token.txt").write_text(token)
    print(f"\nclient_id: {client_id}")
    print(f"access_token: {token[:40]}…")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
