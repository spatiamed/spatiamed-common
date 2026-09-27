#!/usr/bin/env python3
"""SP3 live proof: OpenEmrAdapter.create_patient against the harness OpenEMR.
Run with the harness up and probe_patient_create.py done once (it registers the
gitignored patient_probe_client.json), using the same compose project the harness
was started under (db() shells out to `docker compose exec`):
    COMPOSE_PROJECT_NAME=<project> uv run python harness/openemr/verify_patient_create.py
(a) create -> created=True + id; (b) repeat -> created=False, same id, ONE DB row;
(c) read-back name/DOB/sex/phone; (d) marker identifier persisted? (FINDINGS §14 P1
says OpenEMR 8.3.0 drops it, so (b) rests on the phone+family+birthdate fallback).
Exit 1 on any FAIL. Creates one throwaway "Sp3live…" patient with a 90001xxxxx phone.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys
import uuid
from datetime import date

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from setup_client import ensure_keypair  # noqa: E402

from sm_common.integrations import PatientCreate  # noqa: E402
from sm_common.integrations.adapters.fhir_r4 import PATIENT_IDENTIFIER_SYSTEM  # noqa: E402
from sm_common.integrations.adapters.openemr import OpenEmrAdapter  # noqa: E402
from sm_common.phone import hash_phone_for_lookup  # noqa: E402

HERE = pathlib.Path(__file__).parent
BASE = "https://localhost:9300"
FHIR = f"{BASE}/apis/default/fhir"
TOKEN_URL = f"{BASE}/oauth2/default/token"
DOB = date(1985, 3, 12)
# FINDINGS §14 P1 (OpenEMR 8.3.0): the marker identifier is dropped on create.
P1_IDENTIFIER_KEPT = False
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())
    if not ok:
        failures.append(name)


def db(sql: str) -> str:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "mysql", "mariadb", "-uroot", "-popenemr_root",
         "openemr", "-N", "-e", sql],
        cwd=HERE, check=True, capture_output=True, text=True,
    ).stdout.strip()


async def main() -> int:
    client = json.loads((HERE / "patient_probe_client.json").read_text())
    pem, kid = ensure_keypair()
    oe = OpenEmrAdapter(
        base_url=FHIR,
        auth_scheme="private_key_jwt",
        auth_cfg={"token_url": TOKEN_URL, "client_id": client["client_id"],
                  "private_key_pem": pem, "kid": kid,
                  "scopes": "system/Patient.read system/Patient.write"},
        openemr={"timezone": "Asia/Kolkata", "pc_catid": "5", "pc_facility": "3",
                 "pc_billing_location": "3",
                 "write_user": {"token_url": TOKEN_URL, "client_id": "unused",
                                "username": "unused", "password": "unused"}},
    )
    await oe._client.aclose()
    oe._client = httpx.AsyncClient(verify=False, timeout=30.0)  # harness self-signed cert only
    fam = f"Sp3live{uuid.uuid4().hex[:6]}"
    phone = f"90001{uuid.uuid4().int % 10**5:05d}"
    before = {p.resource_id for p in await oe.search_patients(phone=phone)}
    req = PatientCreate(patient_marker=uuid.uuid4(), family=fam, given=["Asha"], birth_date=DOB,
                        gender="F", phone=phone, exclude_ids=frozenset(i for i in before if i))

    first = await oe.create_patient(req)
    check("(a) create returns created=True and an id", first.created and bool(first.resource_id))
    second = await oe.create_patient(req)
    check("(b) repeat returns created=False, same id",
          (second.created, second.resource_id) == (False, first.resource_id))
    rows = int(db(f"SELECT COUNT(*) FROM patient_data WHERE lname = '{fam}';"))
    check("(b) exactly one DB row", rows == 1, f"rows={rows}")
    back = await oe.get_patient(first.resource_id)
    check("(c) read-back name", back is not None and fam in back.name_token)
    check("(c) read-back DOB", back is not None and back.birth_date == DOB)
    check("(c) read-back sex", back is not None and back.gender == "F")
    check("(c) read-back phone",
          back is not None and back.phone_hash == hash_phone_for_lookup(phone, ""))
    raw = (await oe._client.get(f"{FHIR}/Patient/{first.resource_id}",
                                headers=await oe._headers())).json()
    kept = any(i.get("system") == PATIENT_IDENTIFIER_SYSTEM for i in raw.get("identifier") or [])
    check("(d) marker identifier persisted agrees with FINDINGS §14 P1",
          kept == P1_IDENTIFIER_KEPT, f"persisted={'yes' if kept else 'no'}")
    await oe.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
