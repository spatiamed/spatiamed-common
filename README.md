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
- feedparser >= 6.0
- fastapi >= 0.115 (optional, only for `sm_common.fastapi_guard`)

### Removed in v0.6.0

`VisitCompletedPayload` no longer carries the legacy `department_code` / `department_name` / `doctor_name`
fields. They were `""`-defaults that no producer emitted (since QueueCare #99) and no consumer ever read;
their presence forced QueueCare to `exclude=` the trio at every `visit.completed` emit site because
`build_envelope` does an unconditional `model_dump`. Producers can now call `build_envelope` directly.

### Removed in v0.4.0

Dead exports removed after verifying no consumer imported them: `encryption.encrypt_for_transport` /
`decrypt_from_transport`, `dlt.validate_dlt_template_id`, `compliance.is_within_allowed_hours`, and the
entire `truecaller` module.
