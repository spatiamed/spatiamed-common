# spatiamed-common

Shared Python library used by Platform API, QueueCare, and CareLoop services.

## Dev Commands

```bash
pip install -e ".[dev]"       # Install with dev deps
pytest tests/ -v              # Run all tests
pytest tests/test_phone.py -v # Run single module tests
```

## Architecture

This is a **library, not a service**. No Dockerfile, no deployment, no API, no database.

```
sm_common/
├── phone.py              # Phone normalization + hashing (SHA-256)
├── encryption.py         # AES-256-GCM field encryption + Fernet transport
├── webhook_auth.py       # HMAC-SHA256 webhook signing + verification
├── gupshup.py            # WhatsApp Business API client (Gupshup)
├── msg91.py              # SMS API client (MSG91)
├── truecaller.py         # Truecaller Business caller ID registration
├── dlt.py                # DLT template ID validation
├── compliance.py         # TCCCPR call timing rules
└── nmc_guardrails.py     # NMC advertising content checker
```

## Key Constraints

- Library must NOT load env vars — callers pass config in
- No business logic, no DB models, no API routes
- Python >=3.12, deps: cryptography, httpx, pytz
- All async clients use httpx.AsyncClient
- Tests are unit tests only — mock httpx, no external API calls

## Critical Contracts

- `hash_phone("+91 98765 43210", salt) == hash_phone("9876543210", salt)` — always true
- `encrypt_field → decrypt_field` round-trip preserves plaintext
- `sign_webhook → verify_webhook` round-trip succeeds
- NMC checker blocks "best doctor", passes "experienced doctor"
- TCCCPR rejects calls at 9:30 PM IST
