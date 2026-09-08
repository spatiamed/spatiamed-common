#!/usr/bin/env python
"""Prove get_patient against the running OpenEMR, not a mock."""

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
FHIR = "https://localhost:9300/apis/default/fhir"


async def main() -> int:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    adapter = FhirR4Adapter(
        base_url=FHIR,
        auth_scheme="bearer",
        auth_cfg={"bearer_token": (HERE / "token.txt").read_text().strip()},
        hash_salt="harness-salt",
    )
    adapter._client = httpx.AsyncClient(verify=ctx, timeout=30.0)

    appts, _ = await adapter.list_appointments_modified_since("", dt.date.today())
    external_id = appts[0].patient.mrn
    patient = await adapter.get_patient(external_id)
    await adapter.close()

    print(f"appointment patient ref: {external_id}")
    print(f"get_patient -> {patient}")
    ok = patient is not None and bool(patient.phone_hash)
    print("RESULT:", "PASS — resolution chain closes" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
