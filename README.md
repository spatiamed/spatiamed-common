# spatiamed-common

Shared Python utilities for SpatiaMed platform services (Platform API, QueueCare, CareLoop).

## Installation

```bash
# From GitHub (pinned to tag)
pip install spatiamed-common @ git+https://github.com/spatiamed/spatiamed-common.git@v0.6.0

# With the optional FastAPI server-side guard (sm_common.fastapi_guard)
pip install "spatiamed-common[fastapi] @ git+https://github.com/spatiamed/spatiamed-common.git@v0.6.0"

# Local development
git clone https://github.com/spatiamed/spatiamed-common.git
cd spatiamed-common
pip install -e ".[dev]"
```

## Usage

```python
from sm_common.phone import normalize_phone, hash_phone, hash_phone_e164, hash_normalized
from sm_common.auth import decode_jwt_with_grace
from sm_common.identity import assert_distinct_salts
from sm_common.encryption import encrypt_field, decrypt_field, FieldEncryptor
from sm_common.webhook_auth import sign_webhook, verify_webhook, build_signed_request
from sm_common.internal_http import InternalClient
from sm_common.config_fetch import ConfigSpec, resolve_config, static_fallback
from sm_common.fastapi_guard import verify_internal_secret  # optional [fastapi] extra
from sm_common.gupshup import GupshupClient, GupshupConfig
from sm_common.msg91 import MSG91Client, MSG91Config
from sm_common.dlt import register_template, DLT_TEMPLATE_PATTERN
from sm_common.nmc_guardrails import check_content, has_blocking_violation
```

### Service-to-service auth (`X-Internal-Secret`)

```python
# Client side — injects X-Internal-Secret on every request
async with InternalClient(base_url, secret, timeout=10.0) as client:
    resp = await client.get("/internal/foo", params={"x": 1})
    resp.raise_for_status()

# Server side — FastAPI dependency factory (fail-closed)
require_internal = verify_internal_secret(settings.internal_secret)  # status_code=403 default

@router.post("/internal/foo", dependencies=[Depends(require_internal)])
async def foo() -> ...:
    ...
```

## Identity contract

This repo is the canonical home for the cross-service identity contract. The
invariants below are load-bearing for the CareLoop <-> QueueCare <-> platform-api
join keys; changing any of them requires coordinated adoption across all three.

### `tenant_id` ≡ `hospital_id`

`tenant_id` and `hospital_id` are the **same UUID under two names**. platform-api
*mints* the value as `tenant_id`; QueueCare *persists* the identical value as
`hospital_id`. The rename happens at the `/tenant-created` provisioning seam:

- Producer — platform-api `provisioning_service.py` sends `tenant_id`.
- Consumer — QueueCare `routers/internal.py` assigns it to `Hospital.id`.
- QueueCare audit middleware maps `tenant_id` → `hospital_id`.

Do **not** rename either field, and never generate a fresh id on the QueueCare
side — treat them as one identity. The patient join key is
`(tenant_id, phone_hash)`, which only works because both services share this id
*and* the same phone-hash salt.

### Hash-family salt separation

Patient **phone** hashes (`phone.hash_phone` / `phone.hash_phone_e164`) and
platform-api staff **email** hashes (`HASH_SALT`) belong to **distinct hash
families** and MUST use **different salts**. If they ever shared a salt, an email
hash and a phone hash could be cross-referenced or mistaken for one another — a
silent identity-confusion hazard. Guard this at startup:

```python
from sm_common.identity import assert_distinct_salts

# in the service that holds both salts (platform-api), at boot:
assert_distinct_salts(
    settings.email_hash_salt,
    settings.phone_hash_salt,
    names=["EMAIL_HASH_SALT", "PHONE_HASH_SALT"],
)
```

The domestic (`hash_phone`) and international (`hash_phone_e164`) paths, by
contrast, intentionally **share** the phone salt: they hash disjoint input spaces
(validated 10-digit Indian vs. E.164-normalized international), so their outputs
never collide, and they must stay on one salt so a number is hashed identically
wherever it enters.

### JWT rotation grace window

The shared JWT signing secret is rotated occasionally. To avoid 401ing in-flight
tokens signed with the just-previous secret (up to their TTL), all services should
decode via `auth.decode_jwt_with_grace(token, secret=..., previous_secret=...)`,
which retries the previous secret on `InvalidTokenError` without relaxing any other
validation. Rotate by promoting current → previous, then removing previous once the
old TTL window has elapsed.

## Secret domains & naming contract

SpatiaMed services use several HMAC/auth secrets. Historically CareLoop's
`INTERNAL_SECRET` was overloaded as the service-auth key **and** the
QueueCare-webhook HMAC key **and** the URL-signing key, so one rotation touched
three unrelated trust domains at once (audit XR-06). They are now **split** into
four independent domains with the canonical SSM key names below.

| # | Trust domain | Canonical key (per service) | Signer / verifier | Coupling |
|---|--------------|-----------------------------|-------------------|----------|
| a | Service-to-service auth (`X-Internal-Secret`) | `INTERNAL_SECRET` (shared, same value across platform-api / QueueCare / CareLoop) | `InternalClient` presents it; `verify_internal_secret` checks it | **Shared** across all services — one value everywhere |
| b | QueueCare → CareLoop webhook HMAC | `queuecare/WEBHOOK_SECRET` **==** `careloop/WEBHOOK_HMAC_SECRET` | QueueCare signs (`build_signed_request`); CareLoop verifies (`verify_webhook`) | **Value-coupled PAIR** — two differently-named keys that MUST hold the identical value |
| c | Review-link URL signing (CareLoop ↔ portal) | `careloop/SIGNED_URL_SECRET` | CareLoop signs + verifies (portal may verify) | **Independent**, CareLoop-local |
| d | Caption-token URL signing | `careloop/SIGNED_URL_SECRET` (shares domain **c**) | CareLoop signs + verifies | **Independent**, CareLoop-local |

Notes:

- **(b) is the historical footgun.** QueueCare signs with an env var named
  `WEBHOOK_SECRET`; CareLoop verifies with `WEBHOOK_HMAC_SECRET`. They are
  **different names for the same value**, coupled only by SSM seeding discipline
  (the exact class of bug that already bit prod once — "CARELOOP_WEBHOOK_URL had
  never been seeded"). We deliberately did **not** rename QueueCare's env var (an
  env rename is a coordinated multi-service deploy); instead the pairing is
  documented here and enforced at boot by a self-check (below).
- **(c) and (d) share one `SIGNED_URL_SECRET`** because both are CareLoop-local
  URL/token signatures that no peer service verifies — collapsing them into one
  URL-signing domain is safe and keeps rotation simple. Split them into separate
  keys only if a caption token ever has to be verified by a different service.
- **Safe-increment fallback.** CareLoop's `WEBHOOK_HMAC_SECRET` and
  `SIGNED_URL_SECRET` each **fall back to `INTERNAL_SECRET` when unset**, with a
  loud one-time startup warning naming the unseeded key. So a deploy is
  behavior-identical until ops seeds a distinct value — no coordinated deploy,
  no lockstep. Once seeded, that domain rotates on its own.

### Boot self-check (canary)

`webhook_auth.self_check(secret)` signs a canary payload and verifies it with the
**same** secret at startup, so an empty / whitespace-only / unrenderable secret
fails **loudly at boot** instead of silently 401ing the first real webhook:

```python
from sm_common.webhook_auth import self_check

if not self_check(settings.resolved_webhook_hmac_secret):
    raise RuntimeError("webhook-HMAC secret failed boot self-check (XR-06)")
```

**Honest limit:** this is a *local* roundtrip — signing and verifying with one
secret always agree, so it can **not** detect a cross-service value mismatch
(domain **b**: `queuecare/WEBHOOK_SECRET != careloop/WEBHOOK_HMAC_SECRET`). A true
cross-service canary needs a network call and is out of scope. Symptom of a
value mismatch: the canary passes at boot but real webhooks 401 at runtime.

### Rotation runbook

- **`SIGNED_URL_SECRET` (c+d) — rotate freely.** No peer verifies it; the worst
  case is in-flight review links / caption tokens signed with the old value stop
  verifying (they re-mint on next send). Single-service change.
- **`WEBHOOK_SECRET` ⟷ `WEBHOOK_HMAC_SECRET` (b) — rotate the PAIR together.**
  Seed the new value on **both** QueueCare and CareLoop; because they are two
  names for one value, updating only one side breaks webhook verification.
  CareLoop's `verify_with_rotation` supports a grace window (previous secret
  accepted for `DEFAULT_GRACE_HOURS`) to avoid a hard cutover.
- **`INTERNAL_SECRET` (a) — rotate across ALL services.** It is shared platform-
  wide; every service that presents or verifies `X-Internal-Secret` must get the
  new value together.

## Modules

| Module | Purpose |
|--------|---------|
| `phone.py` | Indian phone normalization + SHA-256 hashing (`hash_normalized` primitive, India `hash_phone`, international `hash_phone_e164`) |
| `auth.py` | `decode_jwt_with_grace` — PyJWT decode with a previous-secret rotation grace window (no FastAPI dep) |
| `identity.py` | Cross-service identity contract: `assert_distinct_salts` startup guard (see "Identity contract" below) |
| `encryption.py` | AES-256-GCM field encryption (`encrypt_field`/`decrypt_field`, `FieldEncryptor`) |
| `webhook_auth.py` | HMAC-SHA256 webhook signing + verification (`sign_webhook`, `verify_webhook`, `build_signed_request`) + `self_check` boot canary (see "Secret domains & naming contract") |
| `internal_http.py` | `InternalClient` — service-to-service httpx client (`X-Internal-Secret`) |
| `config_fetch.py` | `resolve_config` — tenant config from platform-api HTTP + Redis cache + 7-day last-known-good, with a **caller-controlled fail-safe fallback** (no baked-in domain default) |
| `fastapi_guard.py` | `verify_internal_secret` FastAPI dependency factory (optional `[fastapi]` extra) |
| `gupshup.py` | WhatsApp Business API client (Gupshup) |
| `msg91.py` | SMS API client (MSG91) |
| `dlt.py` | TRAI DLT template registration (`register_template`) + `DLT_TEMPLATE_PATTERN` |
| `compliance.py` | TCCCPR call-timing constants (`ALLOWED_START`/`ALLOWED_END`, `MAX_CALLS_PER_*`, `IST`) |
| `nmc_guardrails.py` | NMC advertising content checker |
| `integrations/` | HMS integration layer: `HmsAdapter` interface, canonical types, vendor adapters |

The package ships `py.typed`, so consumers get inline types (no `ignore_missing_imports` needed).

## Bahmni adapter

`vendor="bahmni"` gives two clients over one HTTP core (`BahmniHttp`):

- `build_adapter(cfg)` returns `BahmniAdapter`, the `HmsAdapter` contract (patient search and create,
  appointment write-back and cancel, booking poll, doctor roster, health check).
- `build_bahmni_record_client(cfg)` returns `BahmniRecordClient`, the system-of-record operations
  (patient create/update, visits, encounters with diagnoses and drug orders, appointment services,
  allergies, document filing).

**Transport.** Bahmni REST (`/openmrs/ws/rest/v1/...`) plus the appointments AtomFeed
(`/openmrs/ws/atomfeed/appointments/recent`, walking `prev-archive` back to the cursor). FHIR2 is used
only for allergies; Bahmni's FHIR2 module has no Appointment resource. Timestamps go over the wire as
epoch milliseconds.

**Auth: `basic` only.** OpenMRS supports HTTP Basic with a `JSESSIONID` session cookie, nothing else.
`credentials` takes `username`, `password` and `auth_scheme` (`"basic"`, the default; `"session"` is an
alias). `"api_key"` raises `ValueError` at construction. The session is reused, and a 401 triggers one
re-login and one retry. A second 401 raises `AuthError`, and a 403 is never retried. TLS verification is
on: pass `ca_bundle_pem` (a PEM string) for a private CA. `verify_tls=False` is honoured only together with
`allow_insecure_tls=True`, and is meant for throwaway test boxes only.

**`BahmniConfig`** is built from the integration's `field_mapping` (`BahmniConfig.from_mapping`; unknown
keys are ignored, and the library reads no env vars). Every value is install-specific, so nothing has a
built-in uuid. A key marked "required for X" raises `ConfigError` when X is attempted without it, before
any HTTP call.

| Key | Required | What it is for |
|---|---|---|
| `location_uuid` | **always** (construction) | Location for appointments, visits and encounters |
| `identifier_type_uuid` | **always** (construction) | Patient identifier type of the UHID |
| `identifier_source_uuid` | **always** (construction) | idgen source that mints the UHID on patient create |
| `identifier_prefix` | no | idgen prefix, sent as `identifierPrefix` when set |
| `phone_attribute_name` | no (default `phoneNumber`) | Person attribute NAME that `bahmni/search/patient` searches and returns for phone |
| `phone_attribute_type_uuid` | for create/update with a phone | Person attribute type that stores the phone |
| `marker_attribute_name` + `marker_attribute_type_uuid` | no, but set both or neither | Person attribute holding our patient id, so a retried create finds the earlier patient instead of duplicating. The attribute type must be searchable in Bahmni patient search |
| `default_service_uuid` | for appointment write-back, unless `service_by_practitioner` covers the provider | Appointment service used when no per-provider mapping exists |
| `service_by_practitioner` | no | provider uuid to appointment service uuid |
| `system_provider_identifiers` | no (default `UNKNOWN`, `LABMANAGER`, `LABSYSTEM`, `superman`) | Provider identifiers left out of the doctor roster |
| `opd_visit_type_uuid` | for `start_visit` | Visit type of OPD visits |
| `consultation_encounter_type_uuid` | for `write_encounter` | Encounter type of consultation notes |
| `note_marker_concept_uuid` | for `write_encounter` | Text concept carrying our note id, so a retried write finds the earlier encounter |
| `concept_map` | for each observation key sent | Our observation keys (`chief_complaint`, `history`, `examination`, `advice`, `follow_up`, `vitals_pulse`, `vitals_sbp`, `vitals_dbp`, `vitals_temp`, `vitals_spo2`, `vitals_weight`, `vitals_height`) to concept uuids |
| `drug_other_concept_uuid` | **for drug orders** | Concept for non-coded drug orders (free-text drug name) |
| `drug_quantity_units` | **for drug orders** (no default) | Dosing-unit concept NAME sent as `quantityUnits` (for example `Unit` or `Tablet`). Bahmni rejects drug orders without a valid one |
| `document_visit_type_uuid` + `document_encounter_type_uuid` | for document filing | Visit and encounter types that patient documents are filed under |
| `document_type_concepts` | for each document type filed | Our document type to Bahmni concept uuid |
| `default_provider_uuid` | no | Not read by the library. It is carried for callers, which pick a provider for document filing when the staff member has no practitioner mapping |

**Behaviour to know:**

- Appointment `comments` start with `sm:<booking_id>`. This is the idempotency marker. A marker lookup
  ignores `Cancelled` and `Missed` appointments, so a retry after either one creates a new appointment.
  `hms_booking_id` is always the appointment uuid.
- `appointments/search` returns at most 50 rows and has no paging. The adapter splits the window until each
  response is under the cap, and flags truncation only when a 1-minute window is still full.
- Patient search returns every hit (a household phone can match several patients). Search hits carry no
  birthdate-estimated flag, so `BahmniAdapter` search hits report `birth_date_precision="year"`, and
  `BahmniRecordClient` search hits report `birth_date_estimated=None`. `get_patient` (patientprofile) is
  authoritative.

## Testing

```bash
pytest -q
ruff check .
mypy sm_common
```

## Requirements

- Python >= 3.12
- cryptography >= 46.0
- httpx >= 0.27
- pytz >= 2024.1
- fastapi >= 0.115 (optional, only for `sm_common.fastapi_guard`)

### Changed in v0.26.0

Encounter writes on `BahmniRecordClient`, from behaviour seen on a live Bahmni standard stack with
synthetic data. Bahmni honours a caller-supplied `encounterUuid` on `bahmnicore/bahmniencounter`: a new
uuid creates its own encounter, and a known uuid updates that encounter. A write *without* one is merged
into the matching encounter of the same type, provider and visit, and once two such encounters exist
every uuid-less write fails with a bare HTTP 500 ("More than one encounter matches the criteria").

- **`write_encounter` always sends an `encounterUuid`.** New optional field
  `RecordEncounterInput.encounter_uuid: UUID | None`; `None` uses `note_id`. Each note is now its own
  encounter (previously Bahmni could merge it into the session's encounter). The marker lookup still runs
  first, because re-sending a diagnosis into an existing encounter is also a Bahmni 500. A response naming
  another `encounterUuid` than the one sent raises `VendorRejected(landed=True)`.
- **`write_disposition` always sends an `encounterUuid`** too: new keyword `encounter_uuid: UUID | None`,
  default `event_id`. The disposition is its own encounter and is no longer merged into the consultation.
- **New terminal errors** (both `VendorRejected`, never `TransientError`, exported from
  `sm_common.integrations`), with a stable `code`:
  - `EncounterOutsideVisit` (`code="encounter_outside_visit"`): the encounter time is before the visit's
    start or after its stop. Bahmni answers this with an HTML 500; exactly the visit start is accepted and
    a 1 to 2 s clock skew is enough to trigger it. `write_encounter` and `write_disposition` now check the
    visit's range (read in the same call as the marker lookup) and raise this before any write; a 500 on a
    visit closed meanwhile is diagnosed the same way.
  - `EncounterMatchAmbiguous` (`code="encounter_match_ambiguous"`): a uuid-less `bahmniencounter` write
    (the ADMISSION of `admit` into an open visit, and `transfer`) got a 500 while the visit holds two or
    more unvoided encounters of that type by the same provider. Any other 500 stays `TransientError`.
  - `HmsAdapterError.code` is the new class attribute (`None` on the general types), and
    `TransientError.status_code` carries the HTTP status (`None` for a transport failure).
- **`get_visit` drug names are read per drug order.** The generic encounter order rep no longer names
  `drugNonCoded`: Bahmni's lab orders are plain `Order`s, and a DrugOrder-only field in a generic order
  rep fails the whole read once a visit holds a lab order. The orders are listed as `uuid,orderType`, and
  each drug order's name is read through `GET order/<uuid>` (one extra call per drug order).
- The live harness's stale-visit task run gets its own read timeout (`HARNESS_ADT_STALE_TASK_TIMEOUT_S`,
  default 600 s).

### Added in v0.25.0

`BahmniRecordClient` gains an inpatient (ADT) surface, verified against a live Bahmni standard stack
(bedmanagement 6.0.0, emrapi 1.36.0, ipd 1.1.1) with synthetic data:
- **Wards and beds.** `list_wards`, `get_ward`, `current_bed`, plus master-data creation: `create_ward`,
  `ensure_bed_type`, `add_bed`. Beds are keyed by their integer id (`RecordBed.bed_id`), because Bahmni
  rewrites a bed's uuid when a patient leaves it. Bed numbers are at most 10 characters.
- **Worklists and dispositions.** `adt_worklist("to_admit" | "admitted" | "to_discharge")` and
  `write_disposition`.
- **Admit, transfer, discharge.** `admit`, `transfer`, `discharge` (optionally ending the visit) and
  `end_visit`. Each takes a caller `event_id` and is retry-safe: the encounter is found again by a marker obs,
  and the bed is re-read before any write. Refusals are `ConflictError` with a reason such as
  `bed_occupied`, `already_in_bed`, `visit_changed` or `visit_closed`.
- **`find_by_marker(marker: UUID)`** is a public lookup for the registration marker that `create_patient`
  writes. It fails closed the same way `create_patient` does.
- New record types: `RecordWard`, `RecordBed`, `RecordBedAssignment`, `RecordAdtRow`, `RecordAdtInput`,
  `RecordAdmitResult`.
- New `BahmniConfig` keys (all optional unless the ADT methods are used): `visit_location_uuid`,
  `ipd_visit_type_uuid`, `admission_encounter_type_uuid`, `transfer_encounter_type_uuid`,
  `discharge_encounter_type_uuid`, `discharge_summary_encounter_type_uuid`, `admit_new_ipd_visit`
  (default `true`) and `disposition_codes` (`{"admit": ..., "discharge": ...}` concept names).

Behaviour changes consumers should know about:
- `start_visit` places a new visit at `visit_location_uuid` when it is set (else `location_uuid`), so that
  Bahmni's ADT worklists list it. Encounters keep the login location.
- `RecordEncounterInput.encounter_type_uuid` (optional) overrides the consultation encounter type, for
  example for discharge summaries. An empty value, or one of the configured ADT encounter types, raises
  `ConfigError` before any HTTP call.
- `BahmniConfig.from_mapping` raises `ConfigError` when a mapping field is not a mapping or
  `admit_new_ipd_visit` is not a boolean.

The live harness gains an `--adt` group (`harness/bahmni/adt_checks.py`); each `CHECK` line names the
behaviour topic it verifies.

### Changed in v0.24.0

The Bahmni adapter was rebuilt against a live Bahmni standard stack (see "Bahmni adapter" above). Changes
consumers must handle:
- **Constructor.** It is now `BahmniAdapter(http: BahmniHttp, config: BahmniConfig, hash_salt, transport_key)`.
  The old `(base_url, auth_scheme, hash_salt, transport_key, api_key, username, password)` form is gone.
  Build through `build_adapter(AdapterBuildConfig(...))`.
- **`ConfigError` at build.** `build_adapter` / `build_bahmni_record_client` raise `ConfigError` when
  `field_mapping` lacks `location_uuid`, `identifier_type_uuid` or `identifier_source_uuid`, or when
  `credentials` lack `username`/`password`. Previously the adapter was built and failed later.
- **Auth.** `auth_scheme` now defaults to `"basic"` (it was `"api_key"`), and `"api_key"` raises `ValueError`.
  The value is case-insensitive, and a blank value means `"basic"`.
- **`push_visit_event`** raises `NotFound` when Bahmni has no such appointment (HTTP 404). An identity patient
  search that gets a 404 also raises `NotFound`; it never returns "no hits".
- Search hits report `birth_date_precision="year"` instead of an exact date.
- Drug orders need `drug_quantity_units`.
- The `feedparser` dependency is dropped. The AtomFeed is parsed with the standard library.

`BahmniRecordClient` and `build_bahmni_record_client` are new.

**hms-connector-agent must not bump to 0.24.0 until its defaults change.** It defaults to `HMS_VENDOR=bahmni`
with `HMS_AUTH_SCHEME=api_key`, which now raises at build. It also needs `username`/`password` credentials and
a `field_mapping` holding the required `BahmniConfig` keys.

### Removed in v0.6.0

`VisitCompletedPayload` no longer carries the legacy `department_code` / `department_name` / `doctor_name`
fields. They were `""`-defaults that no producer emitted (since QueueCare #99) and no consumer ever read;
their presence forced QueueCare to `exclude=` the trio at every `visit.completed` emit site because
`build_envelope` does an unconditional `model_dump`. Producers can now call `build_envelope` directly.

### Removed in v0.4.0

Dead exports removed after verifying no consumer imported them: `encryption.encrypt_for_transport` /
`decrypt_from_transport`, `dlt.validate_dlt_template_id`, `compliance.is_within_allowed_hours`, and the
entire `truecaller` module.
