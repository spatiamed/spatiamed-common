# spatiamed-common

Shared Python utilities for SpatiaMed platform services (Platform API, QueueCare, CareLoop).

## Installation

```bash
# From GitHub (pinned to tag)
pip install spatiamed-common @ git+https://github.com/spatiamed/spatiamed-common.git@v0.5.0

# With the optional FastAPI server-side guard (sm_common.fastapi_guard)
pip install "spatiamed-common[fastapi] @ git+https://github.com/spatiamed/spatiamed-common.git@v0.5.0"

# Local development
git clone https://github.com/spatiamed/spatiamed-common.git
cd spatiamed-common
pip install -e ".[dev]"
```

## Usage

```python
from sm_common.phone import normalize_phone, hash_phone
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

## Modules

| Module | Purpose |
|--------|---------|
| `phone.py` | Indian phone normalization + SHA-256 hashing |
| `encryption.py` | AES-256-GCM field encryption (`encrypt_field`/`decrypt_field`, `FieldEncryptor`) |
| `webhook_auth.py` | HMAC-SHA256 webhook signing + verification (`sign_webhook`, `verify_webhook`, `build_signed_request`) |
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

### Removed in v0.4.0

Dead exports removed after verifying no consumer imported them: `encryption.encrypt_for_transport` /
`decrypt_from_transport`, `dlt.validate_dlt_template_id`, `compliance.is_within_allowed_hours`, and the
entire `truecaller` module.
