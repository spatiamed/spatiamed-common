#!/usr/bin/env python3
"""SP3 probes: what OpenEMR 8.3.0 does with a FHIR Patient create and a
family+birthdate search. Run with the harness up and setup_client.py done, using
the same compose project the harness was started under (db()/enable() shell out to
`docker compose exec`, which otherwise targets the default project):
    COMPOSE_PROJECT_NAME=<project> uv run python harness/openemr/probe_patient_create.py
Prints one verdict line per question (P1..P5). Creates throwaway patients whose
family names start with "Sp3probe" and a fresh 90000xxxxx phone each run.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys
import uuid

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from probe_write_scopes import enable  # noqa: E402
from setup_client import JWKS_URI, ensure_keypair  # noqa: E402

from sm_common.integrations.auth import build_auth_headers  # noqa: E402

HERE = pathlib.Path(__file__).parent
BASE = "https://localhost:9300"
FHIR = f"{BASE}/apis/default/fhir"
TOKEN_URL = f"{BASE}/oauth2/default/token"
MARKER_SYSTEM = "https://spatiamed.com/patient"
SCOPES = "system/Patient.read system/Patient.write"
CLIENT_FILE = HERE / "patient_probe_client.json"  # gitignored


def db(sql: str) -> str:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "mysql", "mariadb", "-uroot", "-popenemr_root",
         "openemr", "-N", "-e", sql],
        cwd=HERE, check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def split_row(fhir_id: str) -> str:
    return db(f"SELECT fname, mname, lname FROM patient_data WHERE uuid = "
              f"UNHEX(REPLACE('{fhir_id}', '-', ''));")


def register(client: httpx.Client) -> dict:
    if CLIENT_FILE.exists():
        return json.loads(CLIENT_FILE.read_text())
    r = client.post(
        f"{BASE}/oauth2/default/registration",
        json={
            "application_type": "private",
            "client_name": "SpatiaMed SP3 Patient Probe",
            "grant_types": ["client_credentials"],
            "token_endpoint_auth_method": "private_key_jwt",
            "redirect_uris": ["https://localhost:9300/unused"],
            "jwks_uri": JWKS_URI,
            "scope": SCOPES,
            "contacts": ["harness@spatiamed.test"],
        },
        timeout=30.0,
    )
    r.raise_for_status()
    CLIENT_FILE.write_text(json.dumps(r.json(), indent=2))
    enable(r.json()["client_id"])
    return r.json()


def patient(text: str, family: str, given: list[str], dob: str, phone: str, marker: str,
            gender: str | None = "female") -> dict:
    res: dict = {
        "resourceType": "Patient",
        "name": [{"use": "official", "text": text, "family": family, "given": given}],
        "birthDate": dob,
        "telecom": [{"system": "phone", "value": phone}],
        "identifier": [{"system": MARKER_SYSTEM, "value": marker}],
    }
    if gender:
        res["gender"] = gender
    return res


def entries(resp: httpx.Response) -> list[dict]:
    return resp.json().get("entry") or []


def created_id(resp: httpx.Response) -> str | None:
    """The new Patient's FHIR id: a FHIR `id`, else OpenEMR's `uuid`."""
    if resp.status_code not in (200, 201) or not resp.content:
        return None
    body = resp.json()
    return body.get("id") or body.get("uuid")


async def main() -> int:
    pem, kid = ensure_keypair()
    with httpx.Client(verify=False) as c:
        reg = register(c)
    async with httpx.AsyncClient(verify=False, timeout=30.0) as ac:
        h = await build_auth_headers(ac, "private_key_jwt", {
            "token_url": TOKEN_URL, "client_id": reg["client_id"],
            "private_key_pem": pem, "kid": kid, "scopes": SCOPES,
        })
        h = {**h, "Content-Type": "application/fhir+json"}
        run = uuid.uuid4().hex[:6]
        phone = f"90000{uuid.uuid4().int % 10**5:05d}"

        # Create response shape: OpenEMR 8.3.0 answers {"pid", "uuid"}, not a Patient.
        marker = str(uuid.uuid4())
        fam = f"Sp3probe{run}"
        r = await ac.post(f"{FHIR}/Patient", headers=h,
                          json=patient(f"Asha {fam}", fam, ["Asha"], "1985-03-12", phone, marker))
        print(f"create -> HTTP {r.status_code}")
        if r.status_code not in (200, 201):
            print(f"  body keys: {sorted(r.json()) if r.content else []}")
            return 1
        rid = created_id(r)
        print(f"create response: body keys {sorted(r.json())}, "
              f"Location header: {'present' if 'location' in r.headers else 'absent'}")

        # P1: our marker identifier on read-back and via identifier search.
        got = (await ac.get(f"{FHIR}/Patient/{rid}", headers=h)).json()
        kept = any(i.get("system") == MARKER_SYSTEM and i.get("value") == marker
                   for i in got.get("identifier") or [])
        by_marker = await ac.get(f"{FHIR}/Patient", headers=h,
                                 params={"identifier": f"{MARKER_SYSTEM}|{marker}"})
        n_marker = len(entries(by_marker))
        print(f"P1 identifier kept on read-back: {'yes' if kept else 'no'}; "
              f"identifier search hits: {n_marker}; read-back identifier systems: "
              f"{sorted({i.get('system') for i in got.get('identifier') or []})}")
        print(f"   read-back name[0] keys: {sorted((got.get('name') or [{}])[0])}")

        # P2: how name[] lands in patient_data.
        print(f"P2 text={'Asha ' + fam!r} -> patient_data(fname,mname,lname)={split_row(rid)!r}")
        for text, family, given in [
            (f"Asha Devi {fam}", fam, ["Asha", "Devi"]),
            (f"Asha Van Der {fam}", f"Van Der {fam}", ["Asha"]),
        ]:
            rr = await ac.post(f"{FHIR}/Patient", headers=h, json=patient(
                text, family, given, "1985-03-12", phone, str(uuid.uuid4())))
            print(f"P2 text={text!r} -> patient_data(fname,mname,lname)="
                  f"{split_row(created_id(rr))!r}")
        three = patient(f"Asha Kumari Devi {fam}k", f"{fam}k", ["Asha", "Kumari", "Devi"],
                        "1985-03-12", phone, str(uuid.uuid4()))
        r3g = await ac.post(f"{FHIR}/Patient", headers=h, json=three)
        back3 = (await ac.get(f"{FHIR}/Patient/{created_id(r3g)}", headers=h)).json()
        print(f"P2 three given names -> HTTP {r3g.status_code}; patient_data(fname,mname,lname)="
              f"{split_row(created_id(r3g))!r}; read-back given: "
              f"{back3['name'][0].get('given')}")
        structured = patient("", f"{fam}s", ["Asha"], "1985-03-12", phone, str(uuid.uuid4()))
        del structured["name"][0]["text"]
        rs = await ac.post(f"{FHIR}/Patient", headers=h, json=structured)
        errs_s = sorted(rs.json().get("validationErrors") or {}) if rs.content else []
        print(f"P2 family+given, no text -> HTTP {rs.status_code}; "
              f"patient_data(fname,mname,lname)={split_row(created_id(rs) or '')!r}; "
              f"validation error fields: {errs_s}")
        text_only = patient(f"Asha Rao {fam}t", "", [], "1985-03-12", phone, str(uuid.uuid4()))
        text_only["name"] = [{"use": "official", "text": f"Asha Rao {fam}t"}]
        rt = await ac.post(f"{FHIR}/Patient", headers=h, json=text_only)
        errs = sorted(rt.json().get("validationErrors") or {}) if rt.content else []
        print(f"P2 name.text only (no family/given) -> HTTP {rt.status_code}; "
              f"validation error fields: {errs}")

        # P3: no gender.
        r3 = await ac.post(f"{FHIR}/Patient", headers=h, json=patient(
            f"Ravi {fam}x", f"{fam}x", ["Ravi"], "1990-01-01", phone, str(uuid.uuid4()),
            gender=None))
        errs3 = sorted(r3.json().get("validationErrors") or {}) if r3.content else []
        print(f"P3 create without gender -> HTTP {r3.status_code}; "
              f"validation error fields: {errs3}")

        # P4: family + birthdate honoured (and NOT ignored: a wrong date must miss).
        hit = await ac.get(f"{FHIR}/Patient", headers=h,
                           params={"family": fam, "birthdate": "eq1985-03-12"})
        miss = await ac.get(f"{FHIR}/Patient", headers=h,
                            params={"family": fam, "birthdate": "eq1985-03-13"})
        n_hit, n_miss = len(entries(hit)), len(entries(miss))
        honoured = "yes" if n_hit >= 1 and n_miss == 0 else "no"
        print(f"P4 family+birthdate: right date -> {n_hit}, wrong date -> {n_miss} "
              f"(honoured: {honoured})")
        vdb = await ac.get(f"{FHIR}/Patient", headers=h,
                           params={"family": f"Van Der {fam}", "birthdate": "eq1985-03-12"})
        print(f"P4 multi-word family 'Van Der …' + birthdate -> {len(entries(vdb))} hits")
        # P5: telecom. The create above sent a phone with no `use`.
        cols = "phone_cell, phone_home"
        no_use = db(f"SELECT CONCAT_WS('|', {cols}) FROM patient_data WHERE uuid = "
                    f"UNHEX(REPLACE('{rid}', '-', ''));")
        print(f"P5 telecom without use -> stored: {'yes' if phone in no_use else 'no'}; "
              f"read-back telecom: {'present' if got.get('telecom') else 'absent'}")
        for use in ("mobile", "home"):
            ph = f"90000{uuid.uuid4().int % 10**5:05d}"
            res = patient(f"Asha {fam}{use[0]}", f"{fam}{use[0]}", ["Asha"], "1985-03-12",
                          ph, str(uuid.uuid4()))
            res["telecom"][0]["use"] = use
            ru = await ac.post(f"{FHIR}/Patient", headers=h, json=res)
            uid = created_id(ru)
            row = db(f"SELECT phone_cell = '{ph}', phone_home = '{ph}' FROM patient_data "
                     f"WHERE uuid = UNHEX(REPLACE('{uid}', '-', ''));")
            n_tel = len(entries(await ac.get(f"{FHIR}/Patient", headers=h,
                                             params={"telecom": ph})))
            print(f"P5 telecom use={use} -> HTTP {ru.status_code}; "
                  f"(in phone_cell, in phone_home)={row!r}; telecom search hits: {n_tel}")

        # Conditional create, first time: a fresh marker + If-None-Exist must still create.
        fam5 = f"{fam}n"
        marker5 = str(uuid.uuid4())
        ine5 = {**h, "If-None-Exist": f"identifier={MARKER_SYSTEM}|{marker5}"}
        r5 = await ac.post(f"{FHIR}/Patient", headers=ine5, json=patient(
            f"Asha {fam5}", fam5, ["Asha"], "1985-03-12", phone, marker5))
        print(f"If-None-Exist first create (fresh marker) -> HTTP {r5.status_code} "
              f"id={'present' if created_id(r5) else 'none'}")

        # Conditional create, repeat: does If-None-Exist on the marker return the same id?
        # Count lname=fam rows before/after: the P2 "Asha Devi" case shares lname=fam.
        count_sql = f"SELECT COUNT(*) FROM patient_data WHERE lname = '{fam}';"
        before = int(db(count_sql))
        ine = {**h, "If-None-Exist": f"identifier={MARKER_SYSTEM}|{marker}"}
        r6 = await ac.post(f"{FHIR}/Patient", headers=ine, json=patient(
            f"Asha {fam}", fam, ["Asha"], "1985-03-12", phone, marker))
        id6 = created_id(r6)
        print(f"If-None-Exist repeat -> HTTP {r6.status_code} "
              f"id_same={id6 is not None and id6 == rid}")
        added = int(db(count_sql)) - before
        print(f"rows added by the repeat (lname={fam!r}): {added} (0 = deduped)")

        # P4 match mode (run last, so the suffixed families above exist): a truncated
        # or lower-cased family still hitting means starts-with.
        for label, probe_fam in (("<fam minus last char>", fam[:-1]),
                                 ("<fam lower-cased>", fam.lower()),
                                 ("Der <fam> (inner words of 'Van Der <fam>')", f"Der {fam}")):
            pr = await ac.get(f"{FHIR}/Patient", headers=h,
                              params={"family": probe_fam, "birthdate": "eq1985-03-12"})
            lnames = sorted({e["resource"]["name"][0].get("family") for e in entries(pr)})
            print(f"P4 family={label!r} -> {len(entries(pr))} hits, "
                  f"distinct families returned: {len(lnames)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
