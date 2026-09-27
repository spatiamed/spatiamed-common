# SP3 (spatiamed-common v0.15.0): patient create, name+DOB search, loud failures — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship sm_common v0.15.0: `HmsAdapter.create_patient` (FHIR + OpenEMR), `search_patients` by family name + exact birth date, `search_patients` / `get_patient` raising `TransientError` instead of returning "nothing", and the qc-4 `dob.validate` age-bound fix.

**Architecture:** Additive on the adapter contract. `create_patient` is a non-abstract base method that raises `WriteNotSupported`, overridden by `FhirR4Adapter` (idempotent on a `https://spatiamed.com/patient|<patient_id>` identifier: pre-search, then `POST /Patient` with `If-None-Exist`), and — only if the task-1 probe shows OpenEMR drops that identifier — by an `OpenEmrAdapter` pre-search fallback on phone + family + birthdate that ignores `exclude_ids`. The pre-search is a single overridable hook (`_find_existing_created`) so the fallback is one method.

**Tech Stack:** Python 3.12, httpx, pytest + pytest-asyncio (`asyncio_mode = "auto"`), respx / `httpx.MockTransport`, the OpenEMR 8.3.0 docker harness in `harness/openemr/`.

**Spec:** `QueueCare/docs/superpowers/specs/2026-09-27-sp3-hms-patient-creation-design.md` (rev 3, all DPs DECIDED 2026-09-27) — §1 Corrections 2 and 7, §5.1–§5.2, §8, §10 (sm_common list), §12 (spatiamed-common tasks). Read §8 before starting. qc-4 is from `/Users/animeshjha/Dev/spatiaMed/.superpowers/hms-223/w1-triage.md`.

**Where this runs:** a fresh worktree of `spatiamed-common` off `origin/main` (f69338b, v0.14.0), branch `feat/sp3-patient-create`. Never in the `spatiamed-common/` main checkout.

**Release order (spec §8):** this plan ships first. Its last task bumps the version to 0.15.0 and commits. **Tagging `v0.15.0` and pushing are the controller's/user's job, not the implementer's.** QueueCare task 1 (pin bump) cannot start until the tag exists.

## Global Constraints

Copied from the spec's binding facts; every task implicitly includes them.

- B6: Data migrations are never Alembic, because `entrypoint.sh` stamps head when a migration fails. (No migration in this repo; carried because this plan's behaviour change reaches QueueCare's ingest and kiosk on its pin bump.)
- B5: `patients.phone_hash` is globally UNIQUE, and `patients` has no `hospital_id` and no RLS. Households are deferred.
- B8: Any `hms_integrations` row on a tenant forces `STAFF_TASK` (`intake.py`, `assignment_policy.py`).
- B4: "Create in HMS" is only for **new** patients. Staff must supply a real DOB and surname. Nothing estimated is ever written to the HMS.
- §8.1: `search_patients` and `get_patient` raise `TransientError` on an HTTP or transport error, instead of returning `[]` / `None`. A 404 from `get_patient` still returns `None`.
- §8.2: `search_patients` gains `family: str | None` and `birth_date: date | None`. FHIR: `Patient?family=…&birthdate=eq<YYYY-MM-DD>`, always used together. A name-only search is refused (a `ValueError`).
- §8.3: `create_patient` is **not** abstract. The default raises `WriteNotSupported`, so the five non-FHIR adapters need no change. `PatientCreate(patient_marker: UUID, family: str, given: list[str], birth_date: date, gender: str, phone: str | None, exclude_ids: frozenset[str])`, validated in `__post_init__`: `family` ≥ 2 characters, `birth_date` a real date. `PatientCreateResult(resource_id: str, mrn: str | None, created: bool)`.
- §5.2: FHIR `name[].text` is mandatory on OpenEMR (structured fields are ignored; "First Name must not be empty"), and `birthDate` is mandatory.
- Refusal strings are PHI-safe: use `_refusal(resp)` (HTTP status + OperationOutcome issue codes), never body text, never names/DOBs/phones in an exception message or log line.
- Live OpenEMR tests/scripts are opt-in (need the harness up) and read credentials from the harness's **gitignored** files (`client.json`, `private_key.pem`, `jwks/jwks.json`, `write_probe_client.json`, `patient_probe_client.json`). Never commit a credential file or paste a secret value into any file.
- Commands: `uv sync --extra dev` once, then `uv run pytest` (bare, from the repo root), `uv run ruff check .`, `uv run mypy sm_common`. Harness scripts: `uv run python harness/openemr/<script>.py` with the harness up (`cd harness/openemr && docker compose up -d`).
- Commit messages end with exactly: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. No push, no tag.

## Review Focus

The five uncovered inputs most likely to bite someone, each pinned by a test in the owning task.

1. **A token-endpoint failure inside `search_patients` / `get_patient` / `create_patient`** (OpenEMR's OAuth server down while FHIR is up). Expected: `TransientError`, never a raw `httpx`/`KeyError` that QueueCare turns into a 500 or — worse — swallows as "no candidates". Test: `test_search_raises_transient_when_the_token_fetch_fails` (Task 3), `test_create_raises_transient_when_the_token_fetch_fails` (Task 5).
2. **A search that answers 200 with a non-JSON or non-Bundle body** (proxy error page). Expected: `TransientError`, not `[]` (an empty list unlocks Create in HMS). Test: `test_search_non_json_body_is_transient` (Task 3).
3. **A create that returns 201 with a body lacking `id`** (FINDINGS §7.2: a 2xx is not evidence). Expected: `VendorRejected` with `landed=True` (the record may exist), never a result with an empty resource id. Test: `test_create_2xx_without_id_is_vendor_rejected_landed` (Task 5).
4. **A family name with surrounding whitespace or a single letter after trimming** (`" K "`). Expected: `PatientCreate` refuses it (`ValueError`), so no adapter ever posts an initial as a surname. Test: `test_patient_create_rejects_a_one_letter_surname_after_trim` (Task 4).
5. **A marker pre-search that the vendor refuses with 4xx** (server does not support `identifier` search). Expected: fall through to the conditional create (the `If-None-Exist` header is then our only guard), matching the appointment write path; a 5xx is `TransientError`. Test: `test_create_marker_search_4xx_still_posts_with_if_none_exist` (Task 5).

---

## File structure

| File | Responsibility |
|---|---|
| `harness/openemr/probe_patient_create.py` (new) | Task 1's four live probes; prints a verdict per question. |
| `harness/openemr/FINDINGS.md` (modify) | New §14 recording the probe results and the (d) identifier verdict. |
| `harness/openemr/.gitignore` (modify) | Add `patient_probe_client.json`. |
| `sm_common/identity/dob.py` (modify) | qc-4: age bound on the latest possible birthday for `year`/`month`. |
| `sm_common/integrations/exceptions.py` (modify) | New `SearchNotSupported`. |
| `sm_common/integrations/canonical_types.py` (modify) | `PatientCreate`, `PatientCreateResult`. |
| `sm_common/integrations/hms_adapter.py` (modify) | `search_patients` kwargs, base `create_patient` default. |
| `sm_common/integrations/adapters/fhir_r4.py` (modify) | Loud search/get, family+birthdate search, `create_patient`, `_find_existing_created` hook. |
| `sm_common/integrations/adapters/openemr.py` (modify, Task 6 only if needed) | Fallback `_find_existing_created`. |
| `sm_common/integrations/__init__.py` (modify) | Export the new names. |
| `harness/openemr/verify_patient_create.py` (new) | §8.5 live proof (a)–(d). |
| `tests/test_identity_dob.py`, `tests/integrations/adapters/test_fhir_r4_patient_search.py` (new), `tests/integrations/adapters/test_fhir_r4_patient_create.py` (new), `tests/integrations/test_patient_create_types.py` (new), `tests/integrations/adapters/test_openemr_patient_create.py` (new, Task 6), `tests/integrations/adapters/test_fhir_r4_rest.py` (modify) | Tests. |

---

### Task 1: Live probes against the harness OpenEMR (decides Tasks 5–6)

The spec lists four facts nothing in the repo has measured. Every later create task depends on them; run this first.

**Files:**
- Create: `harness/openemr/probe_patient_create.py`
- Modify: `harness/openemr/FINDINGS.md` (append `## 14. Patient create + name/DOB search (measured <date>)`)
- Modify: `harness/openemr/.gitignore` (add `patient_probe_client.json`)

**Interfaces:**
- Consumes: `setup_client.ensure_keypair()`, `setup_client.JWKS_URI`, `probe_write_scopes.enable()` (existing harness helpers).
- Produces: four recorded verdicts that later tasks read from FINDINGS §14:
  - **P1 identifier kept?** — `yes` ⇒ Task 6 is skipped; `no` ⇒ Task 6 is required.
  - **P2 text split** — how `name[].text` maps to `fname`/`lname` for `"Asha Rao"` and `"Asha Devi Rao"` and `"Asha Van Der Berg"`; Task 5's `_name_text()` follows it.
  - **P3 sex required?** — informational (our form always sends it).
  - **P4 `family` + `birthdate` honoured?** — `yes` ⇒ Task 3's query shape stands; `no` ⇒ Task 3 adds the client-side filter described in its Step 3b.

- [ ] **Step 1: Bring the harness up and confirm it answers**

Run:
```bash
cd harness/openemr && docker compose up -d && cd ../..
uv run python harness/openemr/setup_client.py   # idempotent; writes the gitignored client files
curl -sk https://localhost:9300/apis/default/fhir/metadata | head -c 200
```
Expected: a CapabilityStatement JSON prefix. If the harness cannot start on this machine, stop and write status `NEEDS_CONTEXT` ("harness OpenEMR not runnable here; Tasks 5–6 need P1/P2/P4") — do not guess the probe results.

- [ ] **Step 2: Write the probe script**

```python
#!/usr/bin/env python3
"""SP3 probes: what OpenEMR 8.3.0 does with a FHIR Patient create and a
family+birthdate search. Run with the harness up and setup_client.py done:
    uv run python harness/openemr/probe_patient_create.py
Prints one verdict line per question (P1..P4). Creates throwaway patients whose
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

        # P1 + P2: create with the marker; read it back; inspect patient_data.
        marker = str(uuid.uuid4())
        fam = f"Sp3probe{run}"
        r = await ac.post(f"{FHIR}/Patient", headers=h,
                          json=patient(f"Asha {fam}", fam, ["Asha"], "1985-03-12", phone, marker))
        print(f"create -> HTTP {r.status_code}")
        if r.status_code not in (200, 201):
            print(f"  body keys: {sorted(r.json()) if r.content else []}")
            return 1
        rid = r.json().get("id")
        got = (await ac.get(f"{FHIR}/Patient/{rid}", headers=h)).json()
        kept = any(i.get("system") == MARKER_SYSTEM and i.get("value") == marker
                   for i in got.get("identifier") or [])
        by_marker = (await ac.get(f"{FHIR}/Patient", headers=h,
                                  params={"identifier": f"{MARKER_SYSTEM}|{marker}"})).json()
        n_marker = len(by_marker.get("entry") or [])
        print(f"P1 identifier kept on read-back: {'yes' if kept else 'no'}; "
              f"identifier search hits: {n_marker}")

        for text, family, given in [
            (f"Asha Devi {fam}", fam, ["Asha", "Devi"]),
            (f"Asha Van Der {fam}", f"Van Der {fam}", ["Asha"]),
        ]:
            m = str(uuid.uuid4())
            rr = await ac.post(f"{FHIR}/Patient", headers=h,
                               json=patient(text, family, given, "1985-03-12", phone, m))
            pid = (await ac.get(f"{FHIR}/Patient/{rr.json().get('id')}", headers=h)).json()
            row = db(f"SELECT fname, mname, lname FROM patient_data WHERE uuid = "
                     f"UNHEX(REPLACE('{pid.get('id')}', '-', ''));")
            print(f"P2 text={text!r} -> patient_data(fname,mname,lname)={row!r}")

        # P3: no gender.
        r3 = await ac.post(f"{FHIR}/Patient", headers=h, json=patient(
            f"Ravi {fam}x", f"{fam}x", ["Ravi"], "1990-01-01", phone, str(uuid.uuid4()), gender=None))
        print(f"P3 create without gender -> HTTP {r3.status_code}")

        # P4: family + birthdate honoured (and NOT ignored: a wrong date must miss).
        hit = (await ac.get(f"{FHIR}/Patient", headers=h,
                            params={"family": fam, "birthdate": "eq1985-03-12"})).json()
        miss = (await ac.get(f"{FHIR}/Patient", headers=h,
                             params={"family": fam, "birthdate": "eq1985-03-13"})).json()
        n_hit, n_miss = len(hit.get("entry") or []), len(miss.get("entry") or [])
        print(f"P4 family+birthdate: right date -> {n_hit}, wrong date -> {n_miss} "
              f"(honoured: {'yes' if n_hit >= 1 and n_miss == 0 else 'no'})")

        # Conditional create: does If-None-Exist on the marker return the same id?
        r5 = await ac.post(f"{FHIR}/Patient",
                           headers={**h, "If-None-Exist": f"identifier={MARKER_SYSTEM}|{marker}"},
                           json=patient(f"Asha {fam}", fam, ["Asha"], "1985-03-12", phone, marker))
        print(f"If-None-Exist repeat -> HTTP {r5.status_code} id_same={r5.json().get('id') == rid if r5.content else 'no body'}")
        count = db(f"SELECT COUNT(*) FROM patient_data WHERE lname = '{fam}';")
        print(f"rows with lname={fam!r}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 3: Run it and record**

Run: `uv run python harness/openemr/probe_patient_create.py`
Expected: seven verdict lines. Do not paste phone numbers into FINDINGS (they are synthetic, but keep the habit): record the verdicts only.

- [ ] **Step 4: Write FINDINGS §14**

Append to `harness/openemr/FINDINGS.md`:

```markdown
## 14. Patient create + name/DOB search (measured <YYYY-MM-DD>, SP3 task 1)

Probed with `probe_patient_create.py` (system token, `system/Patient.write`).

- **P1 — our identifier on create:** <kept | dropped> on read-back; `identifier=<system>|<marker>`
  search returns <n> hits. Consequence: <marker works as the idempotency key | OpenEmrAdapter
  must use the phone+family+birthdate fallback (SP3 sm_common Task 6)>.
- **P2 — `name[].text` split:** "Asha Devi X" → (<fname>, <mname>, <lname>); "Asha Van Der X" →
  (<…>). Consequence for `FhirR4Adapter._name_text`: <…>.
- **P3 — sex on create:** without `gender` → HTTP <status>. <required | optional>.
- **P4 — `family` + `birthdate`:** right date → <n>, wrong date → <m>: <honoured | ignored>.
- **If-None-Exist on Patient:** repeat create → HTTP <status>, same id: <yes|no>; lname row count <n>.
- **Scope note:** a create needs `system/Patient.write` on the integration's FHIR client. The
  QueueCare live test's `_credentials()` and every tenant's saved scopes today carry only
  `system/Patient.read …`; Create in HMS needs the write scope added (QueueCare plan, live task).
```

Add `patient_probe_client.json` to `harness/openemr/.gitignore`.

- [ ] **Step 5: Commit**

```bash
git add harness/openemr/probe_patient_create.py harness/openemr/FINDINGS.md harness/openemr/.gitignore
git commit -m "harness(openemr): SP3 probes — Patient create identifier, name split, sex, family+birthdate

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: qc-4 — `dob.validate` bounds the age on the latest possible birthday

`validate` runs `age_on(anchor, reference) > 120`. For `year` the anchor is 1 July, for `month` the 15th, so a real birth late in the stated period is wrongly rejected at the boundary (`"1905"` on 2026-09-24).

**Files:**
- Modify: `sm_common/identity/dob.py` (`validate`, and the module docstring's "Valid means…" bullet)
- Test: `tests/test_identity_dob.py`

**Interfaces:**
- Produces: `validate(dob: Dob, *, reference: date) -> None` — same signature; now accepts any `year`/`month` DOB for which *some* real date inside the stated period is ≤ 120 years before `reference`.

- [ ] **Step 1: Write the failing tests** (append after `test_120_years_is_the_limit`, which stays unchanged)

```python
def test_year_only_1905_is_valid_at_the_boundary():
    """qc-4: a Nov/Dec-1905 birth is 120 on 2026-09-24, so "1905" is a valid year."""
    dob = parse_dob("1905", reference=REF)
    assert dob.precision == "year"


def test_year_only_1904_is_still_too_old():
    with pytest.raises(DobError):
        parse_dob("1904", reference=REF)


def test_month_only_at_the_boundary_is_valid():
    # 1905-09-30 is still 120 on 2026-09-24; the 15th anchor made it 121.
    assert parse_dob("1905-09", reference=REF).precision == "month"


def test_month_only_past_the_boundary_is_too_old():
    with pytest.raises(DobError):
        parse_dob("1905-08", reference=REF)


def test_exact_boundary_is_unchanged_by_the_partial_rule():
    with pytest.raises(DobError):
        parse_dob("1906-09-23", reference=REF)  # 121 on REF (birthday passed)
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_identity_dob.py -k "boundary or too_old" -v`
Expected: `test_year_only_1905_is_valid_at_the_boundary` and `test_month_only_at_the_boundary_is_valid` FAIL with `DobError: date of birth is more than 120 years ago`; the other three PASS.

- [ ] **Step 3: Implement**

Replace `validate` in `sm_common/identity/dob.py`:

```python
def _latest_in_period(dob: Dob) -> date:
    """The latest real birthday the stated precision allows: the period's last day."""
    v = dob.value
    if dob.precision == "year":
        return date(v.year, 12, 31)
    if dob.precision == "month":
        return date(v.year, v.month, calendar.monthrange(v.year, v.month)[1])
    return v


def validate(dob: Dob, *, reference: date) -> None:
    v = dob.value
    if dob.precision == "year":
        future = v.year > reference.year
    elif dob.precision == "month":
        future = (v.year, v.month) > (reference.year, reference.month)
    else:
        future = v > reference
    if future or v > reference:
        raise DobError("date of birth is after the reference day")
    # The age limit, like the future check, runs on the components: a partial DOB
    # is valid when ANY real date inside it is within the limit, i.e. its last day.
    if age_on(min(_latest_in_period(dob), reference), reference) > MAX_AGE_YEARS:
        raise DobError(f"date of birth is more than {MAX_AGE_YEARS} years ago")
```

Update the module docstring bullet to: "Valid means a real calendar date, not after the reference day, and at most 120 years before it. For ``month``/``year`` both checks run on the components (the age limit on the period's last day)…".

- [ ] **Step 4: Run the whole DOB suite**

Run: `uv run pytest tests/test_identity_dob.py -v`
Expected: all PASS (the round-trip loops included).

- [ ] **Step 5: Commit**

```bash
git add sm_common/identity/dob.py tests/test_identity_dob.py
git commit -m "fix(identity): year/month DOB age bound uses the period's last day (qc-4)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `search_patients` / `get_patient` fail loudly; family + birth-date search

**Files:**
- Modify: `sm_common/integrations/exceptions.py` (add `SearchNotSupported`)
- Modify: `sm_common/integrations/hms_adapter.py` (`search_patients` signature + default)
- Modify: `sm_common/integrations/adapters/fhir_r4.py` (`_patient_query`, `search_patients`, `get_patient`, new `_token_headers`)
- Modify: `sm_common/integrations/__init__.py` (export `SearchNotSupported`)
- Modify: `tests/integrations/adapters/test_fhir_r4_rest.py` (`test_find_patient_http_error_returns_none` becomes `..._raises_transient`)
- Create: `tests/integrations/adapters/test_fhir_r4_patient_search.py`

**Interfaces:**
- Produces:
  - `class SearchNotSupported(HmsAdapterError)` — "this vendor cannot run that search". QueueCare maps it to 422 `search_not_supported`.
  - `HmsAdapter.search_patients(self, phone_hash=None, mrn=None, abha_id=None, phone=None, family: str | None = None, birth_date: date | None = None) -> list[CanonicalPatient]`. Base default: `family`/`birth_date` given ⇒ `SearchNotSupported`; otherwise unchanged (wraps `find_patient`).
  - `FhirR4Adapter.search_patients(...)` same signature. Raises `ValueError` when exactly one of `family`/`birth_date` is given, or when `family`/`birth_date` is combined with `phone`/`mrn`/`abha_id` (one search per call). Raises `TransientError` on any transport error, non-2xx, token failure or unreadable body.
  - `FhirR4Adapter.get_patient(external_id) -> CanonicalPatient | None`: `None` for empty id, 404 or a non-Patient body; `TransientError` for everything else that isn't 200.
  - `FhirR4Adapter._token_headers(self, what: str) -> dict[str, str]` — `_headers()` with httpx/ValueError/KeyError mapped to `TransientError` (the roster's existing pattern, now shared).

- [ ] **Step 1: Write the failing tests**

`tests/integrations/adapters/test_fhir_r4_patient_search.py`:

```python
"""SP3 §8.1–§8.2: a search that fails must never look like "no such patient"."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.exceptions import SearchNotSupported, TransientError

pytestmark = pytest.mark.asyncio


def _adapter(handler) -> FhirR4Adapter:
    a = FhirR4Adapter(base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"})
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _bundle(*resources):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources]}


def _pat(pid="p1"):
    return {"resourceType": "Patient", "id": pid, "name": [{"text": "Asha Rao"}], "birthDate": "1985-03-12"}


async def test_search_5xx_raises_transient():
    a = _adapter(lambda r: httpx.Response(503, text="down"))
    with pytest.raises(TransientError):
        await a.search_patients(phone="9876543210")


async def test_search_transport_error_raises_transient():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(TransientError):
        await _adapter(handler).search_patients(phone="9876543210")


async def test_search_non_json_body_is_transient():
    a = _adapter(lambda r: httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(TransientError):
        await a.search_patients(phone="9876543210")


async def test_search_raises_transient_when_the_token_fetch_fails():
    a = FhirR4Adapter(
        base_url="https://hms.example/fhir",
        auth_scheme="oauth2_client_credentials",
        auth_cfg={"token_url": "https://hms.example/token", "client_id": "c", "client_secret": "s"},
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(TransientError):
        await a.search_patients(phone="9876543210")


async def test_family_and_birthdate_query_shape():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=_bundle(_pat()))

    found = await _adapter(handler).search_patients(family="Rao", birth_date=date(1985, 3, 12))
    assert seen == {"family": "Rao", "birthdate": "eq1985-03-12"}
    assert [p.resource_id for p in found] == ["p1"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"family": "Rao"},
        {"birth_date": date(1985, 3, 12)},
        {"family": "Rao", "birth_date": date(1985, 3, 12), "phone": "9876543210"},
    ],
)
async def test_name_without_dob_or_mixed_search_is_refused(kwargs):
    def handler(request):
        raise AssertionError("must not call the vendor")

    with pytest.raises(ValueError):
        await _adapter(handler).search_patients(**kwargs)


async def test_empty_bundle_is_still_an_empty_list():
    a = _adapter(lambda r: httpx.Response(200, json=_bundle()))
    assert await a.search_patients(phone="9876543210") == []


async def test_get_patient_5xx_raises_transient():
    with pytest.raises(TransientError):
        await _adapter(lambda r: httpx.Response(502)).get_patient("p1")


async def test_get_patient_transport_error_raises_transient():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TransientError):
        await _adapter(handler).get_patient("p1")


async def test_get_patient_404_is_still_none():
    assert await _adapter(lambda r: httpx.Response(404)).get_patient("gone") is None


async def test_find_patient_inherits_the_loud_failure():
    with pytest.raises(TransientError):
        await _adapter(lambda r: httpx.Response(500)).find_patient(mrn="x")


async def test_base_default_refuses_name_dob_search():
    adapter = GenericRestAdapter({"base_url": "https://x", "list_appointments_path": "/a"})
    with pytest.raises(SearchNotSupported):
        await adapter.search_patients(family="Rao", birth_date=date(1985, 3, 12))
```

In `tests/integrations/adapters/test_fhir_r4_rest.py`, replace `test_find_patient_http_error_returns_none` with:

```python
@pytest.mark.asyncio
async def test_find_patient_http_error_raises_transient():
    """SP3 §8.1: an outage must not read as "no such patient"."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    with pytest.raises(TransientError):
        await _adapter(handler).find_patient(mrn="pat-1")
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/integrations/adapters/test_fhir_r4_patient_search.py tests/integrations/adapters/test_fhir_r4_rest.py -v`
Expected: FAIL — `ImportError: cannot import name 'SearchNotSupported'` first; after adding the class, the loud-failure tests fail with "DID NOT RAISE".

- [ ] **Step 3: Implement**

`exceptions.py`, append:

```python
class SearchNotSupported(HmsAdapterError):  # noqa: N818
    """This vendor cannot run the requested patient search (e.g. name + DOB). Terminal."""
```

`hms_adapter.py`: change `search_patients`:

```python
    async def search_patients(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
        phone: str | None = None,
        family: str | None = None,
        birth_date: date | None = None,
    ) -> list[CanonicalPatient]:
        """Every candidate the HMS returns. ... (keep the existing paragraphs)

        ``family`` + ``birth_date`` (always together) is a name + exact-DOB search.
        Adapters with no list-returning search cannot run it and raise
        SearchNotSupported rather than answer "nobody" — an empty result is what
        unlocks Create in HMS downstream.
        """
        if family is not None or birth_date is not None:
            raise SearchNotSupported(f"{self.vendor_name}: name + DOB search is not supported")
        found = await self.find_patient(phone_hash=phone_hash, mrn=mrn, abha_id=abha_id, phone=phone)
        return [found] if found is not None else []
```
(import `SearchNotSupported` from `sm_common.integrations.exceptions`.)

`fhir_r4.py`: add the shared token helper and rewrite the two methods:

```python
    async def _token_headers(self, what: str) -> dict[str, str]:
        """_headers() with every token failure typed. build_auth_headers already raises
        AuthError/TransientError for what it classifies; anything else (a 404 token_url,
        a non-JSON or token-less body) is still transient, never a raw error."""
        try:
            return await self._headers()
        except httpx.HTTPError as exc:
            raise TransientError(f"FhirR4Adapter.{what}: token: {exc}") from exc
        except (ValueError, KeyError) as exc:
            raise TransientError(f"FhirR4Adapter.{what}: unreadable token response ({exc!r})") from exc

    def _patient_query(
        self,
        phone_hash: str | None,
        mrn: str | None,
        abha_id: str | None,
        phone: str | None,
        family: str | None = None,
        birth_date: date | None = None,
    ) -> dict | None:  # type: ignore[type-arg]
        if family is not None or birth_date is not None:
            if not family or birth_date is None:
                # A name-only search returns half a town (spec §8.2).
                raise ValueError("family and birth_date are searched together")
            if phone or mrn or abha_id:
                raise ValueError("one patient search per call")
            return {"family": family.strip(), "birthdate": f"eq{birth_date.isoformat()}"}
        # (existing MRN / ABHA / phone / hash branches unchanged)
        ...

    async def search_patients(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
        phone: str | None = None,
        family: str | None = None,
        birth_date: date | None = None,
    ) -> list[CanonicalPatient]:
        params = self._patient_query(phone_hash, mrn, abha_id, phone, family, birth_date)
        if params is None:
            return []
        headers = await self._token_headers("search_patients")
        try:
            resp = await self._client.get(f"{self._base}/Patient", params=params, headers=headers)
        except httpx.HTTPError as exc:
            # SP3 §8.1: raised, never []. "No candidates" unlocks Create in HMS.
            raise TransientError(f"search_patients: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise TransientError(f"search_patients {_refusal(resp)}")
        try:
            bundle = resp.json()
        except ValueError as exc:
            raise TransientError("search_patients: non-JSON body (HTTP 200)") from exc
        if not isinstance(bundle, dict) or bundle.get("resourceType", "Bundle") != "Bundle":
            raise TransientError("search_patients: body is not a Bundle")
        return [
            self._patient_to_canonical(e.get("resource", {}))
            for e in bundle.get("entry", []) or []
            if e.get("resource", {}).get("resourceType", "Patient") == "Patient"
        ]

    async def find_patient(self, phone_hash=None, mrn=None, abha_id=None, phone=None):
        # unchanged body; it now inherits the loud failure from search_patients
        ...

    async def get_patient(self, external_id: str) -> CanonicalPatient | None:
        if not external_id:
            return None
        headers = await self._token_headers("get_patient")
        try:
            resp = await self._client.get(f"{self._base}/Patient/{external_id}", headers=headers)
        except httpx.HTTPError as exc:
            raise TransientError(f"get_patient: {exc.__class__.__name__}") from exc
        if resp.status_code in (404, 410):
            return None
        if resp.status_code != 200:
            raise TransientError(f"get_patient {_refusal(resp)}")
        try:
            resource = resp.json()
        except ValueError as exc:
            raise TransientError("get_patient: non-JSON body (HTTP 200)") from exc
        if resource.get("resourceType") != "Patient":
            return None
        return self._patient_to_canonical(resource)
```

Only `FhirR4Adapter` overrides `search_patients` at v0.14.0 (`git grep -n "def search_patients" sm_common` → `hms_adapter.py`, `fhir_r4.py`; `OpenEmrAdapter` inherits), so no other adapter's signature needs the new kwargs. Re-run that grep before starting; update any new override.

Refactor `fetch_doctor_roster`'s own token `try` to call `self._token_headers("fetch_doctor_roster")` (same behaviour; its existing tests pin it).

- [ ] **Step 3b (only if FINDINGS §14 P4 = "ignored"):** OpenEMR ignores `family`/`birthdate`, so the server returns unrelated patients. Add to the end of `FhirR4Adapter.search_patients`, before returning, when `family` was given:

```python
        if family is not None:
            fam = family.strip().casefold()
            return [
                p for p in found
                if p.birth_date == birth_date and p.birth_date_precision == "exact"
                and fam in (p.name_token or "").casefold().split()
            ]
```
and a test `test_family_birthdate_results_are_filtered_client_side` with a Bundle holding one matching and one non-matching patient. Skip this step when P4 = "honoured".

- [ ] **Step 4: Run the suites**

Run: `uv run pytest tests/integrations -v`
Expected: all PASS. If any other existing test asserted `[]`/`None` on an error (grep: `git grep -n "search_patients\|get_patient" tests`), update it to expect `TransientError` in this commit and name it in the commit body.

- [ ] **Step 5: Commit**

```bash
git add sm_common tests
git commit -m "feat(hms)!: patient search/get raise TransientError; family + birthdate search

search_patients and get_patient swallowed HTTP errors and returned []/None, so an
HMS outage read as 'no such patient' — which under SP3 unlocks Create in HMS.
BREAKING for callers that relied on the silence (QueueCare pins and adapts).

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `PatientCreate` / `PatientCreateResult` and the base `create_patient`

**Files:**
- Modify: `sm_common/integrations/canonical_types.py`
- Modify: `sm_common/integrations/hms_adapter.py`
- Modify: `sm_common/integrations/__init__.py`
- Create: `tests/integrations/test_patient_create_types.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class PatientCreate:
    patient_marker: UUID           # our patients.id; the idempotency key
    family: str                    # stored stripped
    given: list[str]               # stripped, empties dropped
    birth_date: date               # exact by construction
    gender: Literal["M", "F", "O"]
    phone: str | None              # plaintext; None for a phone-less patient
    exclude_ids: frozenset[str] = frozenset()

@dataclass(frozen=True)
class PatientCreateResult:
    resource_id: str
    mrn: str | None
    created: bool

HmsAdapter.create_patient(self, patient: PatientCreate) -> PatientCreateResult  # default: WriteNotSupported
```

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pytest

from sm_common.integrations import PatientCreate, PatientCreateResult
from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.exceptions import WriteNotSupported


def _pc(**over):
    kw = dict(patient_marker=uuid4(), family="Rao", given=["Asha"], birth_date=date(1985, 3, 12),
              gender="F", phone="9876543210")
    kw.update(over)
    return PatientCreate(**kw)


def test_valid_create_is_normalised():
    p = _pc(family="  Rao ", given=[" Asha ", "", "Devi"])
    assert p.family == "Rao" and p.given == ["Asha", "Devi"] and p.exclude_ids == frozenset()


@pytest.mark.parametrize("family", ["R", " K ", "", "  "])
def test_patient_create_rejects_a_one_letter_surname_after_trim(family):
    with pytest.raises(ValueError):
        _pc(family=family)


def test_birth_date_must_be_a_date_not_a_datetime():
    with pytest.raises(ValueError):
        _pc(birth_date=datetime(1985, 3, 12))


def test_birth_date_rejects_a_string():
    with pytest.raises(ValueError):
        _pc(birth_date="1985-03-12")


def test_gender_must_be_m_f_o():
    with pytest.raises(ValueError):
        _pc(gender="female")


def test_exclude_ids_is_frozen():
    p = _pc(exclude_ids=frozenset({"a"}))
    assert p.exclude_ids == frozenset({"a"})


def test_result_shape():
    r = PatientCreateResult(resource_id="p1", mrn=None, created=True)
    assert (r.resource_id, r.mrn, r.created) == ("p1", None, True)


@pytest.mark.asyncio
async def test_default_create_patient_is_write_not_supported():
    adapter = GenericRestAdapter({"base_url": "https://x", "list_appointments_path": "/a"})
    with pytest.raises(WriteNotSupported):
        await adapter.create_patient(_pc())
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/integrations/test_patient_create_types.py -v`
Expected: FAIL with `ImportError: cannot import name 'PatientCreate'`.

- [ ] **Step 3: Implement**

`canonical_types.py` (imports: `from dataclasses import dataclass, field`, `from datetime import date, datetime`, `from typing import Literal`, `from uuid import UUID` — reuse the ones present):

```python
@dataclass(frozen=True)
class PatientCreate:
    """A NEW patient to register in the HMS (SP3 §5, B4: exact DOB, real surname).

    ``patient_marker`` is our patients.id and every adapter's idempotency key.
    ``exclude_ids`` are HMS ids that must never be read as "the record our earlier
    attempt created": the ids seen before our first POST plus ids staff confirmed
    are not this patient (spec §5.1). Only the OpenEMR no-marker fallback reads it.
    """

    patient_marker: UUID
    family: str
    given: list[str]
    birth_date: date
    gender: Literal["M", "F", "O"]
    phone: str | None
    exclude_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        family = (self.family or "").strip()
        if len(family) < 2:
            # Our rule, not a vendor's: one letter is an initial, not a surname (B4).
            raise ValueError("family name must be at least 2 characters")
        if not isinstance(self.birth_date, date) or isinstance(self.birth_date, datetime):
            raise ValueError("birth_date must be a calendar date")
        if self.gender not in ("M", "F", "O"):
            raise ValueError("gender must be M, F or O")
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "given", [g.strip() for g in self.given if g and g.strip()])
        object.__setattr__(self, "exclude_ids", frozenset(self.exclude_ids))


@dataclass(frozen=True)
class PatientCreateResult:
    resource_id: str
    mrn: str | None
    # False when the call found the record an earlier attempt already created.
    created: bool
```

`hms_adapter.py` (import `PatientCreate`, `PatientCreateResult`, `WriteNotSupported`):

```python
    async def create_patient(self, patient: PatientCreate) -> PatientCreateResult:
        """Register a NEW patient in the HMS. Idempotent on ``patient.patient_marker``.

        Deliberately not abstract: bahmni, mocdoc, generic_rest, generic_db and
        csv_import have no create route and inherit this refusal (spec §8.3).
        Raises ConflictError (several prior records carry our marker),
        TransientError, AuthError, VendorRejected or WriteNotSupported.
        """
        raise WriteNotSupported(f"{self.vendor_name}: patient create is not supported")
```

Export `PatientCreate`, `PatientCreateResult`, `SearchNotSupported` from `sm_common/integrations/__init__.py` (import lists and `__all__`).

- [ ] **Step 4: Run**

Run: `uv run pytest tests/integrations -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sm_common tests
git commit -m "feat(hms): PatientCreate / PatientCreateResult and a WriteNotSupported create_patient default

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `FhirR4Adapter.create_patient`

**Files:**
- Modify: `sm_common/integrations/adapters/fhir_r4.py`
- Create: `tests/integrations/adapters/test_fhir_r4_patient_create.py`

**Interfaces:**
- Consumes: `PatientCreate`, `PatientCreateResult` (Task 4), `_token_headers` (Task 3), FINDINGS §14 P2 (name text shape).
- Produces:
  - `PATIENT_IDENTIFIER_SYSTEM = "https://spatiamed.com/patient"` (module constant; changing it orphans every earlier create).
  - `FhirR4Adapter.create_patient(patient) -> PatientCreateResult`.
  - Hook `FhirR4Adapter._find_existing_created(self, patient, headers) -> list[dict]` — the records an earlier attempt of ours created. FHIR: `GET Patient?identifier=<system>|<marker>`; 4xx ⇒ `[]` (server can't answer; `If-None-Exist` is then the guard); 5xx/transport ⇒ `TransientError`. Task 6 overrides it on OpenEMR.
  - `FhirR4Adapter._name_text(patient) -> str` — `" ".join([*given, family])` unless P2 says otherwise.

- [ ] **Step 1: Write the failing tests**

```python
"""SP3 §5.2 / §8.4: FHIR Patient create, idempotent on our marker identifier."""

from __future__ import annotations

import json
from datetime import date
from uuid import UUID

import httpx
import pytest

from sm_common.integrations import PatientCreate
from sm_common.integrations.adapters.fhir_r4 import PATIENT_IDENTIFIER_SYSTEM, FhirR4Adapter
from sm_common.integrations.exceptions import (
    AuthError, ConflictError, TransientError, VendorRejected, WriteNotSupported,
)

pytestmark = pytest.mark.asyncio
MARKER = UUID("00000000-0000-0000-0000-0000000000a1")
TOKEN = f"{PATIENT_IDENTIFIER_SYSTEM}|{MARKER}"


def _adapter(handler) -> FhirR4Adapter:
    a = FhirR4Adapter(base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"})
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _pc(phone="9876543210"):
    return PatientCreate(patient_marker=MARKER, family="Rao", given=["Asha", "Devi"],
                         birth_date=date(1985, 3, 12), gender="F", phone=phone)


def _bundle(*resources):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources]}


def _created(id_="p-new", mrn="MRN-7"):
    return {"resourceType": "Patient", "id": id_,
            "identifier": [{"system": PATIENT_IDENTIFIER_SYSTEM, "value": str(MARKER)}, {"value": mrn}]}


async def test_create_carries_marker_if_none_exist_and_name_text():
    seen = {}

    def handler(request):
        if request.method == "GET":
            assert request.url.params["identifier"] == TOKEN
            return httpx.Response(200, json=_bundle())
        seen["body"] = json.loads(request.content)
        seen["ine"] = request.headers.get("If-None-Exist")
        return httpx.Response(201, json=_created())

    r = await _adapter(handler).create_patient(_pc())
    body = seen["body"]
    assert body["resourceType"] == "Patient"
    assert body["name"] == [{"use": "official", "text": "Asha Devi Rao", "family": "Rao", "given": ["Asha", "Devi"]}]
    assert body["birthDate"] == "1985-03-12" and body["gender"] == "female"
    assert body["telecom"] == [{"system": "phone", "value": "+919876543210"}]
    assert body["identifier"] == [{"system": PATIENT_IDENTIFIER_SYSTEM, "value": str(MARKER)}]
    assert seen["ine"] == f"identifier={TOKEN}"
    assert (r.resource_id, r.mrn, r.created) == ("p-new", "MRN-7", True)


async def test_phone_less_patient_has_no_telecom():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle())
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_created())

    await _adapter(handler).create_patient(_pc(phone=None))
    assert "telecom" not in seen["body"]


async def test_a_prior_marker_hit_returns_created_false_without_posting():
    def handler(request):
        if request.method == "POST":
            raise AssertionError("must not POST when our record already exists")
        return httpx.Response(200, json=_bundle(_created("p-old")))

    r = await _adapter(handler).create_patient(_pc())
    assert (r.resource_id, r.created) == ("p-old", False)


async def test_several_marker_hits_raise_conflict():
    def handler(request):
        return httpx.Response(200, json=_bundle(_created("a"), _created("b")))

    with pytest.raises(ConflictError) as ei:
        await _adapter(handler).create_patient(_pc())
    assert ei.value.landed is True


async def test_create_marker_search_4xx_still_posts_with_if_none_exist():
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(400, json={"resourceType": "OperationOutcome", "issue": [{"code": "not-supported"}]})
        posted.append(request.headers.get("If-None-Exist"))
        return httpx.Response(201, json=_created())

    r = await _adapter(handler).create_patient(_pc())
    assert posted == [f"identifier={TOKEN}"] and r.created is True


async def test_marker_search_5xx_is_transient_and_never_posts():
    def handler(request):
        if request.method == "POST":
            raise AssertionError("no create without a completed pre-search")
        return httpx.Response(503)

    with pytest.raises(TransientError):
        await _adapter(handler).create_patient(_pc())


@pytest.mark.parametrize(
    ("status", "exc"),
    [(404, WriteNotSupported), (405, WriteNotSupported), (401, AuthError), (403, AuthError),
     (409, ConflictError), (429, TransientError), (500, TransientError), (422, VendorRejected)],
)
async def test_post_status_mapping(status, exc):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle())
        return httpx.Response(status, json={"resourceType": "OperationOutcome", "issue": [{"code": "invalid"}]})

    with pytest.raises(exc) as ei:
        await _adapter(handler).create_patient(_pc())
    assert "Rao" not in str(ei.value) and "9876543210" not in str(ei.value)  # PHI-safe


async def test_post_transport_error_is_transient():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle())
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TransientError):
        await _adapter(handler).create_patient(_pc())


async def test_create_2xx_without_id_is_vendor_rejected_landed():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle())
        return httpx.Response(201, json={"resourceType": "Patient"})

    with pytest.raises(VendorRejected) as ei:
        await _adapter(handler).create_patient(_pc())
    assert ei.value.landed is True


async def test_conditional_create_200_empty_body_rereads_by_marker():
    calls = {"get": 0}

    def handler(request):
        if request.method == "GET":
            calls["get"] += 1
            return httpx.Response(200, json=_bundle() if calls["get"] == 1 else _bundle(_created("p-old")))
        return httpx.Response(200, content=b"")

    r = await _adapter(handler).create_patient(_pc())
    assert (r.resource_id, r.created) == ("p-old", False)


async def test_create_raises_transient_when_the_token_fetch_fails():
    a = FhirR4Adapter(
        base_url="https://hms.example/fhir",
        auth_scheme="oauth2_client_credentials",
        auth_cfg={"token_url": "https://hms.example/token", "client_id": "c", "client_secret": "s"},
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(TransientError):
        await a.create_patient(_pc())
```

(If P2 shows OpenEMR needs a different `text` shape, adjust `test_create_carries_marker_if_none_exist_and_name_text`'s expected `text` to it and say why in a comment citing FINDINGS §14.)

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/integrations/adapters/test_fhir_r4_patient_create.py -v`
Expected: FAIL with `ImportError: cannot import name 'PATIENT_IDENTIFIER_SYSTEM'`.

- [ ] **Step 3: Implement** (in `fhir_r4.py`; import `PatientCreate`, `PatientCreateResult`, `AuthError`, `phone_search_variants` is already imported)

```python
# Our patients.id travels on every Patient we create, so a retry finds the one it
# already made. Changing this string orphans every earlier create.
PATIENT_IDENTIFIER_SYSTEM = "https://spatiamed.com/patient"
_FHIR_GENDER = {"M": "male", "F": "female", "O": "other"}


def _e164_india(phone: str) -> str:
    """The `+`-prefixed shape most FHIR servers store (FINDINGS §5)."""
    variants = phone_search_variants(phone)
    plus = [v for v in variants if v.startswith("+")]
    return plus[0] if plus else phone.strip()
```

Methods on `FhirR4Adapter`:

```python
    def _name_text(self, patient: PatientCreate) -> str:
        # OpenEMR ignores structured names and splits `text` (FINDINGS §6, §14 P2).
        return " ".join([*patient.given, patient.family])

    def _patient_create_resource(self, patient: PatientCreate) -> dict:  # type: ignore[type-arg]
        resource: dict = {  # type: ignore[type-arg]
            "resourceType": "Patient",
            "name": [{"use": "official", "text": self._name_text(patient),
                      "family": patient.family, "given": list(patient.given)}],
            "birthDate": patient.birth_date.isoformat(),
            "gender": _FHIR_GENDER[patient.gender],
            "identifier": [{"system": PATIENT_IDENTIFIER_SYSTEM, "value": str(patient.patient_marker)}],
        }
        if patient.phone:
            resource["telecom"] = [{"system": "phone", "value": _e164_india(patient.phone)}]
        return resource

    @staticmethod
    def _mrn_of(resource: dict) -> str | None:  # type: ignore[type-arg]
        for ident in resource.get("identifier") or []:
            system = str(ident.get("system") or "")
            value = str(ident.get("value") or "").strip()
            if value and system != PATIENT_IDENTIFIER_SYSTEM and "abha" not in system.lower() and "ndhm" not in system:
                return value
        return None

    def _create_result(self, resource: dict, *, created: bool) -> PatientCreateResult:  # type: ignore[type-arg]
        rid = resource.get("id")
        if resource.get("resourceType") != "Patient" or not rid:
            # A 2xx is not evidence (FINDINGS §7.2). Keys only: the body may be PHI.
            raise VendorRejected(
                f"vendor response carries no Patient id: keys={sorted(resource)}", landed=True
            )
        return PatientCreateResult(resource_id=str(rid), mrn=self._mrn_of(resource), created=created)

    async def _find_existing_created(self, patient: PatientCreate, headers: dict[str, str]) -> list[dict]:  # type: ignore[type-arg]
        token = f"{PATIENT_IDENTIFIER_SYSTEM}|{patient.patient_marker}"
        try:
            resp = await self._client.get(f"{self._base}/Patient", params={"identifier": token}, headers=headers)
        except httpx.HTTPError as exc:
            raise TransientError(f"create_patient pre-search: {exc.__class__.__name__}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"create_patient pre-search {_refusal(resp)}")
        if resp.status_code >= 400:
            logger.warning("FhirR4Adapter: server refused Patient identifier search (HTTP %s)", resp.status_code)
            return []
        try:
            entries = resp.json().get("entry", []) or []
        except ValueError as exc:
            raise TransientError("create_patient pre-search: non-JSON body") from exc
        return [e["resource"] for e in entries if e.get("resource", {}).get("resourceType") == "Patient"]

    async def create_patient(self, patient: PatientCreate) -> PatientCreateResult:
        headers = await self._token_headers("create_patient")
        existing = await self._find_existing_created(patient, headers)
        if len(existing) > 1:
            raise ConflictError(
                f"{len(existing)} HMS patients already match our earlier create — a human must reconcile",
                landed=True,
            )
        if existing:
            return self._create_result(existing[0], created=False)

        token = f"{PATIENT_IDENTIFIER_SYSTEM}|{patient.patient_marker}"
        try:
            resp = await self._client.post(
                f"{self._base}/Patient",
                json=self._patient_create_resource(patient),
                headers={**headers, "If-None-Exist": f"identifier={token}"},
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"create_patient: {exc.__class__.__name__}") from exc

        if resp.status_code in (404, 405):
            raise WriteNotSupported(f"vendor has no Patient create route (HTTP {resp.status_code})")
        if resp.status_code in (401, 403):
            raise AuthError(f"create_patient {_refusal(resp)}")
        if resp.status_code == 409:
            raise ConflictError(f"create_patient {_refusal(resp)}")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"create_patient {_refusal(resp)}")
        if resp.status_code >= 400:
            raise VendorRejected(f"create_patient {_refusal(resp)}")
        if resp.status_code == 200 and not resp.content:
            again = await self._find_existing_created(patient, headers)
            if len(again) != 1:
                raise TransientError("conditional create returned 200 with no body and no single match")
            return self._create_result(again[0], created=False)
        try:
            body = resp.json()
        except ValueError as exc:
            raise VendorRejected("create_patient: non-JSON 2xx body", landed=True) from exc
        return self._create_result(body, created=resp.status_code == 201)
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/integrations -v && uv run ruff check . && uv run mypy sm_common`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add sm_common tests
git commit -m "feat(hms): FhirR4Adapter.create_patient — marker identifier, pre-search, If-None-Exist

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6 (CONDITIONAL — only if FINDINGS §14 P1 = "dropped"): OpenEMR no-marker fallback

If P1 = "kept", skip this task entirely and record "Task 6 skipped: OpenEMR keeps the identifier (FINDINGS §14)" in the final report.

**Files:**
- Modify: `sm_common/integrations/adapters/openemr.py`
- Create: `tests/integrations/adapters/test_openemr_patient_create.py`

**Interfaces:**
- Consumes: `_find_existing_created` hook (Task 5), `search_patients(phone=…)` and `search_patients(family=…, birth_date=…)` (Task 3).
- Produces: `OpenEmrAdapter._find_existing_created(patient, headers) -> list[dict]` — exact phone-variant + family + birthdate matches (family + birthdate for a phone-less patient) whose id is **not** in `patient.exclude_ids`. Returns resource dicts shaped `{"resourceType": "Patient", "id": …, "identifier": […]}` so `_create_result` works unchanged.

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from datetime import date
from uuid import UUID

import httpx
import pytest

from sm_common.integrations import PatientCreate
from sm_common.integrations.adapters.openemr import OpenEmrAdapter
from sm_common.integrations.exceptions import ConflictError

pytestmark = pytest.mark.asyncio
MARKER = UUID("00000000-0000-0000-0000-0000000000b2")
OE = {"timezone": "Asia/Kolkata", "pc_catid": "5", "pc_facility": "3", "pc_billing_location": "3",
      "write_user": {"token_url": "https://oe/t", "client_id": "c", "username": "u", "password": "p"}}


def _adapter(handler) -> OpenEmrAdapter:
    a = OpenEmrAdapter(base_url="https://oe/apis/default/fhir", auth_scheme="bearer",
                       auth_cfg={"bearer_token": "t"}, openemr=OE)
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _pat(pid, family="Rao", dob="1985-03-12", phone="9876543210"):
    return {"resourceType": "Patient", "id": pid, "name": [{"text": f"Asha {family}", "family": family}],
            "birthDate": dob, "telecom": [{"system": "phone", "value": phone}]}


def _bundle(*r):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": x} for x in r]}


def _pc(exclude=frozenset(), phone="9876543210"):
    return PatientCreate(patient_marker=MARKER, family="Rao", given=["Asha"], birth_date=date(1985, 3, 12),
                         gender="F", phone=phone, exclude_ids=frozenset(exclude))


async def test_fallback_ignores_excluded_ids_so_a_twin_is_not_already_created():
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(1)
            return httpx.Response(201, json={"resourceType": "Patient", "id": "twin-b"})
        return httpx.Response(200, json=_bundle(_pat("twin-a")))

    r = await _adapter(handler).create_patient(_pc(exclude={"twin-a"}))
    assert posted == [1] and (r.resource_id, r.created) == ("twin-b", True)


async def test_fallback_one_new_id_is_our_earlier_record():
    def handler(request):
        if request.method == "POST":
            raise AssertionError("must not create twice")
        return httpx.Response(200, json=_bundle(_pat("pre"), _pat("ours")))

    r = await _adapter(handler).create_patient(_pc(exclude={"pre"}))
    assert (r.resource_id, r.created) == ("ours", False)


async def test_fallback_several_new_ids_raise_conflict():
    def handler(request):
        return httpx.Response(200, json=_bundle(_pat("x"), _pat("y")))

    with pytest.raises(ConflictError):
        await _adapter(handler).create_patient(_pc())


async def test_fallback_requires_exact_family_and_birthdate():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={"resourceType": "Patient", "id": "new"})
        return httpx.Response(200, json=_bundle(_pat("other-dob", dob="1985-03-13"), _pat("other-fam", family="Rai")))

    r = await _adapter(handler).create_patient(_pc())
    assert r.created is True


async def test_phone_less_fallback_searches_family_and_birthdate_only():
    seen = []

    def handler(request):
        if request.method == "GET":
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=_bundle())
        return httpx.Response(201, json={"resourceType": "Patient", "id": "n"})

    await _adapter(handler).create_patient(_pc(phone=None))
    assert seen == [{"family": "Rao", "birthdate": "eq1985-03-12"}]
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/integrations/adapters/test_openemr_patient_create.py -v`
Expected: FAIL — the base marker search finds nothing matching the phone fixtures, so `test_fallback_one_new_id_is_our_earlier_record` hits the POST assertion.

- [ ] **Step 3: Implement** (in `OpenEmrAdapter`; import `PatientCreate`)

```python
    async def _find_existing_created(self, patient: PatientCreate, headers: dict[str, str]) -> list[dict]:  # type: ignore[type-arg]
        """OpenEMR drops our marker identifier on create (FINDINGS §14 P1), so an
        earlier attempt is recognised by exact phone + family + birthdate instead
        (family + birthdate for a phone-less patient). Ids in exclude_ids existed
        before our first POST, or staff confirmed them "not this patient": they are
        never "already created" — without that, twins on one phone with one surname
        and DOB would bind twin B to twin A's chart (spec §5.2)."""
        if patient.phone:
            found = await self.search_patients(phone=patient.phone)
        else:
            found = await self.search_patients(family=patient.family, birth_date=patient.birth_date)
        fam = patient.family.casefold()
        out = []
        for c in found:
            if not c.resource_id or c.resource_id in patient.exclude_ids:
                continue
            if c.birth_date != patient.birth_date or c.birth_date_precision != "exact":
                continue
            if fam not in (c.name_token or "").casefold():
                continue
            out.append({"resourceType": "Patient", "id": c.resource_id,
                        "identifier": [{"value": c.mrn}] if c.mrn else []})
        return out
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/integrations -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sm_common tests
git commit -m "feat(openemr): create_patient pre-search falls back to phone+family+birthdate (marker dropped)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Live harness `verify_patient_create.py` (§8.5 (a)–(d))

**Files:**
- Create: `harness/openemr/verify_patient_create.py`
- Modify: `harness/openemr/FINDINGS.md` (§14: add a "Live proof" paragraph with the pass/fail lines)

**Interfaces:**
- Consumes: `OpenEmrAdapter.create_patient`, `search_patients`, `get_patient`; `patient_probe_client.json` (Task 1, gitignored).

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""SP3 live proof: OpenEmrAdapter.create_patient against the harness OpenEMR.
    uv run python harness/openemr/verify_patient_create.py
(a) create -> 201 + id; (b) repeat -> created=False, same id, ONE DB row;
(c) read-back name/DOB/sex/phone; (d) identifier persisted? Exit 1 on any FAIL.
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
from sm_common.integrations.adapters.openemr import OpenEmrAdapter  # noqa: E402

HERE = pathlib.Path(__file__).parent
BASE = "https://localhost:9300"
FHIR = f"{BASE}/apis/default/fhir"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        failures.append(name)


def db(sql: str) -> str:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "mysql", "mariadb", "-uroot", "-popenemr_root", "openemr", "-N", "-e", sql],
        cwd=HERE, check=True, capture_output=True, text=True,
    ).stdout.strip()


async def main() -> int:
    client = json.loads((HERE / "patient_probe_client.json").read_text())
    pem, kid = ensure_keypair()
    oe = OpenEmrAdapter(
        base_url=FHIR,
        auth_scheme="private_key_jwt",
        auth_cfg={"token_url": f"{BASE}/oauth2/default/token", "client_id": client["client_id"],
                  "private_key_pem": pem, "kid": kid, "scopes": "system/Patient.read system/Patient.write"},
        openemr={"timezone": "Asia/Kolkata", "pc_catid": "5", "pc_facility": "3", "pc_billing_location": "3",
                 "write_user": {"token_url": f"{BASE}/oauth2/default/token", "client_id": "unused",
                                "username": "unused", "password": "unused"}},
    )
    oe._client = httpx.AsyncClient(verify=False, timeout=30.0)  # harness self-signed cert only
    fam = f"Sp3live{uuid.uuid4().hex[:6]}"
    phone = f"90001{uuid.uuid4().int % 10**5:05d}"
    before = set(p.resource_id for p in await oe.search_patients(phone=phone))
    req = PatientCreate(patient_marker=uuid.uuid4(), family=fam, given=["Asha"], birth_date=date(1985, 3, 12),
                        gender="F", phone=phone, exclude_ids=frozenset(i for i in before if i))

    first = await oe.create_patient(req)
    check("(a) create returns created=True and an id", first.created and bool(first.resource_id))
    second = await oe.create_patient(req)
    check("(b) repeat returns created=False, same id", (second.created, second.resource_id) == (False, first.resource_id))
    rows = int(db(f"SELECT COUNT(*) FROM patient_data WHERE lname = '{fam}';"))
    check("(b) exactly one DB row", rows == 1, f"rows={rows}")
    back = await oe.get_patient(first.resource_id)
    check("(c) read-back name", back is not None and fam in back.name_token)
    check("(c) read-back DOB", back is not None and back.birth_date == date(1985, 3, 12))
    check("(c) read-back sex", back is not None and back.gender == "F")
    check("(c) read-back phone", back is not None and bool(back.phone_hash))
    raw = (await oe._client.get(f"{FHIR}/Patient/{first.resource_id}", headers=await oe._headers())).json()
    kept = any(i.get("system") == "https://spatiamed.com/patient" for i in raw.get("identifier") or [])
    print(f"(d) marker identifier persisted: {'yes' if kept else 'no'}  (record in FINDINGS §14)")
    await oe.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run it**

Run: `uv run python harness/openemr/verify_patient_create.py`
Expected: every line `[PASS]`, and (d) agreeing with Task 1's P1. A `(b)` FAIL means duplicates are possible — stop and report `BLOCKED` with the output.

- [ ] **Step 3: Record in FINDINGS §14** ("Live proof <date>: (a)–(c) PASS; (d) <yes|no>").

- [ ] **Step 4: Commit**

```bash
git add harness/openemr/verify_patient_create.py harness/openemr/FINDINGS.md
git commit -m "harness(openemr): live create_patient proof (a)-(d)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Version 0.15.0 and the full gate

**Files:**
- Modify: `pyproject.toml` (`version = "0.15.0"`)
- Modify: `sm_common/__init__.py` (`__version__ = "0.15.0"`)
- Modify: `uv.lock` (regenerated by `uv lock` if it records the project version)

- [ ] **Step 1: Bump both** (`tests/test_setup.py` asserts they agree).

- [ ] **Step 2: Full gate**

Run: `uv sync --extra dev && uv run pytest && uv run ruff check . && uv run mypy sm_common`
Expected: all pass, zero lint/type errors.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml sm_common/__init__.py uv.lock
git commit -m "chore(release): spatiamed-common 0.15.0 — patient create, loud search, qc-4

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: Hand off.** Do NOT run `scripts/release.sh`, tag or push: it edits consumer repos and tags. Report the branch head SHA; the controller opens the PR, merges and tags `v0.15.0`.

---

## Self-review notes

- Spec §8 items 1–5 → Tasks 3, 3, 4, 5+6, 7. §12 sm_common 1–5 → Tasks 1, 3, 4, 5/6, 7+8. qc-4 → Task 2. §10 sm_common bullets: raise on 5xx/transport (T3), name without DOB (T3), family+birthdate shape (T3), get_patient 404 None (T3), marker + If-None-Exist + name text (T5), OpenEMR fallback ignores exclude_ids + several → Conflict (T6), pre-search hit → created=False no POST (T5), several hits → Conflict (T5), 1-char surname (T4), default → WriteNotSupported (T4).
- Spec gap recorded (not in spec): the base `search_patients` must accept `family`/`birth_date`; it raises the new `SearchNotSupported` rather than answer `[]`.
