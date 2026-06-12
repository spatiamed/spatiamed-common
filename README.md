# spatiamed-common

Shared Python utilities for SpatiaMed platform services (Platform API, QueueCare, CareLoop).

## Installation

```bash
# From GitHub (pinned to tag)
pip install spatiamed-common @ git+https://github.com/spatiamed/spatiamed-common.git@v0.2.1

# Local development
git clone https://github.com/spatiamed/spatiamed-common.git
cd spatiamed-common
pip install -e ".[dev]"
```

## Usage

```python
from sm_common.phone import normalize_phone, hash_phone
from sm_common.encryption import encrypt_field, decrypt_field, encrypt_for_transport
from sm_common.webhook_auth import sign_webhook, verify_webhook
from sm_common.gupshup import GupshupClient, GupshupConfig
from sm_common.msg91 import MSG91Client, MSG91Config
from sm_common.truecaller import TruecallerClient, TruecallerConfig
from sm_common.dlt import validate_dlt_template_id
from sm_common.compliance import is_within_allowed_hours
from sm_common.nmc_guardrails import check_content, has_blocking_violation
```

## Modules

| Module | Purpose |
|--------|---------|
| `phone.py` | Indian phone normalization + SHA-256 hashing |
| `encryption.py` | AES-256-GCM field encryption + Fernet transport |
| `webhook_auth.py` | HMAC-SHA256 webhook signing + verification |
| `gupshup.py` | WhatsApp Business API client (Gupshup) |
| `msg91.py` | SMS API client (MSG91) |
| `truecaller.py` | Truecaller Business caller ID registration |
| `dlt.py` | TRAI DLT template ID validation + template registration |
| `compliance.py` | TCCCPR call timing rules |
| `nmc_guardrails.py` | NMC advertising content checker |
| `integrations/` | HMS integration layer: `HmsAdapter` interface, canonical types, vendor adapters |

## Testing

```bash
pytest tests/ -v
```

## Requirements

- Python >= 3.12
- cryptography >= 46.0
- httpx >= 0.27
- pytz >= 2024.1
