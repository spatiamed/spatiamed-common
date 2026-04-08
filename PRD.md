# PRD-006: spatiamed-common (Shared Python Package)

**Version:** 1.0
**Date:** April 2026
**Status:** Not started
**Depends on:** Nothing — this is Phase 0
**Blocks:** PRD-001 (Platform API), PRD-002 (CareLoop), PRD-005 (QueueCare Integration)
**Repo:** `spatiamed/spatiamed-common`

---

## 1. What This Is

A lightweight Python package installed by Platform API, QueueCare, and CareLoop via pip. It contains shared utilities that must be identical across all three services: phone number hashing, PII encryption, webhook authentication, communication provider clients, and compliance checkers.

This is **not a service**. No Dockerfile, no deployment, no API, no database. It's a library. Other services import it:

```python
from sm_common.phone import hash_phone, normalize_phone
from sm_common.webhook_auth import sign_webhook, verify_webhook
from sm_common.encryption import encrypt_field, decrypt_field, encrypt_for_transport
from sm_common.gupshup import GupshupClient
from sm_common.compliance import check_tcccpr_hours, check_nmc_content
```

### Why It Exists

Three services need identical implementations of:

- **Phone hashing** — QueueCare and CareLoop link patients via `SHA-256(HASH_SALT + phone)`. If the normalization or hashing differs by even one character, patients don't match across systems. One implementation, one import.
- **Webhook auth** — QueueCare → CareLoop webhooks use HMAC-SHA256. Both sides must compute the signature identically.
- **PII encryption** — AES-256-GCM for stored fields, Fernet for cross-service transport. Key derivation must match.
- **Provider clients** — Gupshup (WhatsApp), MSG91 (SMS), Truecaller (caller ID) are used by both QueueCare's notification service and CareLoop's campaign engine. One client implementation, not two.
- **Compliance** — NMC advertising guardrails and TCCCPR call timing rules apply to both products. One ruleset.

### What's NOT in This Package

- Business logic (queue management, campaign orchestration, lead scoring)
- Database models or migrations
- API routes or FastAPI dependencies
- Exotel/Sarvam voice integration (architecturally different between QC and CL — see PRD-002 Section 4, PRD-005 Section 7)
- Configuration or environment variable loading (callers pass config in)

---

## 2. Package Structure

```
spatiamed-common/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── sm_common/
│   ├── __init__.py           # Version: __version__ = "0.1.0"
│   ├── phone.py              # Phone normalization + hashing
│   ├── encryption.py         # AES-256-GCM field encryption + Fernet transport
│   ├── webhook_auth.py       # HMAC-SHA256 webhook signing + verification
│   ├── gupshup.py            # WhatsApp Business API client (Gupshup)
│   ├── msg91.py              # SMS API client (MSG91)
│   ├── truecaller.py         # Truecaller Business registration API
│   ├── dlt.py                # DLT template ID validation
│   ├── compliance.py         # TCCCPR call timing + NDNC check
│   └── nmc_guardrails.py     # NMC advertising content checker
└── tests/
    ├── test_phone.py
    ├── test_encryption.py
    ├── test_webhook_auth.py
    ├── test_gupshup.py
    ├── test_compliance.py
    └── test_nmc_guardrails.py
```

---

## 3. Module Specifications

### 3.1 phone.py — Phone Normalization + Hashing

The single most critical module. If QueueCare and CareLoop hash phones differently, the entire cross-product integration breaks.

```python
import hashlib
import re

def normalize_phone(phone: str) -> str:
    """Normalize Indian phone number to 10-digit format.

    Handles: +919876543210, 919876543210, 09876543210, 9876543210,
             +91 98765 43210, 98765-43210, etc.
    Returns: "9876543210" (10 digits, no prefix)
    Raises: ValueError if input doesn't resolve to a valid 10-digit Indian mobile number.
    """
    cleaned = re.sub(r"[\s\-\(\)]+", "", phone.strip())

    if cleaned.startswith("+91"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = cleaned[2:]
    elif cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = cleaned[1:]

    if not re.match(r"^[6-9]\d{9}$", cleaned):
        raise ValueError(f"Invalid Indian mobile number: {phone}")

    return cleaned


def hash_phone(phone: str, salt: str) -> str:
    """Deterministic salted SHA-256 hash for cross-system patient identity.

    Args:
        phone: Raw phone number (any format — will be normalized)
        salt: HASH_SALT environment variable (must be identical across all services)

    Returns:
        64-char hex digest. Identical output for the same phone+salt in any service.
    """
    normalized = normalize_phone(phone)
    return hashlib.sha256(f"{salt}{normalized}".encode()).hexdigest()
```

**Critical contract:** `hash_phone("+91 98765 43210", "mysalt") == hash_phone("9876543210", "mysalt")` must always be true. Any service that hashes phones must use this function — never inline the logic.

### 3.2 encryption.py — PII Encryption

```python
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import os, base64

# --- Field-level encryption (for storage) ---

def encrypt_field(plaintext: str, key: bytes) -> str:
    """AES-256-GCM encrypt a PII field for database storage.

    Args:
        plaintext: The value to encrypt (name, email, phone)
        key: 32-byte encryption key (service-specific ENCRYPTION_KEY)

    Returns:
        Base64-encoded string: "v1:<nonce>:<ciphertext>:<tag>"
        The "v1:" prefix enables future key rotation.
    """
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    encoded = base64.b64encode(nonce + ciphertext).decode()
    return f"v1:{encoded}"


def decrypt_field(encrypted: str, key: bytes) -> str:
    """Decrypt an AES-256-GCM encrypted field.

    Args:
        encrypted: Value from encrypt_field() ("v1:<base64>")
        key: Same key used for encryption

    Returns:
        Original plaintext string.

    Raises:
        ValueError: If version prefix is unrecognized (key rotation needed)
    """
    if not encrypted.startswith("v1:"):
        raise ValueError(f"Unknown encryption version: {encrypted[:3]}")

    raw = base64.b64decode(encrypted[3:])
    nonce, ciphertext = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


# --- Transport encryption (for webhooks between services) ---

def encrypt_for_transport(plaintext: str, transport_key: str) -> str:
    """Fernet-encrypt PII for webhook transit between services.

    Used only in patient.pre_registered events (CareLoop → QueueCare).
    Receiver decrypts immediately and re-encrypts with their own storage key.

    Args:
        plaintext: PII value to send
        transport_key: WEBHOOK_TRANSPORT_KEY (shared Fernet key)
    """
    f = Fernet(transport_key.encode())
    return f.encrypt(plaintext.encode()).decode()


def decrypt_from_transport(ciphertext: str, transport_key: str) -> str:
    """Decrypt PII received via webhook. Then re-encrypt with local storage key."""
    f = Fernet(transport_key.encode())
    return f.decrypt(ciphertext.encode()).decode()
```

### 3.3 webhook_auth.py — HMAC Webhook Authentication

```python
import hmac
import hashlib
import time
import json

def sign_webhook(payload: dict, secret: str) -> tuple[str, int]:
    """Sign a webhook payload for outbound delivery.

    Args:
        payload: The JSON-serializable webhook body
        secret: WEBHOOK_SECRET (shared between sender and receiver)

    Returns:
        (signature_hex, timestamp_unix) — put these in headers:
        X-Webhook-Signature: sha256=<signature_hex>
        X-Webhook-Timestamp: <timestamp_unix>
    """
    timestamp = int(time.time())
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    message = f"{timestamp}.{body}"
    signature = hmac.new(
        secret.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return signature, timestamp


def verify_webhook(body_bytes: bytes, signature_header: str,
                   timestamp_header: str, secret: str,
                   max_age_seconds: int = 300) -> bool:
    """Verify an inbound webhook signature.

    Args:
        body_bytes: Raw request body (bytes)
        signature_header: Value of X-Webhook-Signature header ("sha256=<hex>")
        timestamp_header: Value of X-Webhook-Timestamp header
        secret: WEBHOOK_SECRET
        max_age_seconds: Reject webhooks older than this (replay protection)

    Returns:
        True if signature is valid and timestamp is fresh.
    """
    try:
        timestamp = int(timestamp_header)
    except (ValueError, TypeError):
        return False

    # Replay protection
    if abs(time.time() - timestamp) > max_age_seconds:
        return False

    expected_sig = signature_header.removeprefix("sha256=")
    message = f"{timestamp}.{body_bytes.decode()}"
    computed = hmac.new(
        secret.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_sig, computed)
```

### 3.4 gupshup.py — WhatsApp Business API Client

```python
import httpx
from dataclasses import dataclass

@dataclass
class GupshupConfig:
    api_key: str          # Platform-level Gupshup API key
    app_name: str         # "spatiamed" or tenant-specific sub-app
    base_url: str = "https://api.gupshup.io/wa/api/v1"


class GupshupClient:
    """WhatsApp Business API client via Gupshup.

    Used by both QueueCare notification service and CareLoop campaign engine.
    Callers provide the tenant's assigned WhatsApp number and Gupshup app name.
    """

    def __init__(self, config: GupshupConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def send_template(
        self, to: str, template_id: str, params: list[str],
        source: str, app_name: str | None = None,
    ) -> dict:
        """Send a pre-approved DLT template message.

        Args:
            to: Recipient phone (normalized 10-digit)
            template_id: Gupshup/DLT template identifier
            params: Template parameter values
            source: Sender WhatsApp number (tenant's assigned number)
            app_name: Gupshup sub-app for this tenant (overrides default)
        """
        response = await self._client.post(
            f"{self.config.base_url}/msg",
            headers={"apikey": self.config.api_key},
            data={
                "channel": "whatsapp",
                "source": source,
                "destination": f"91{to}",
                "src.name": app_name or self.config.app_name,
                "template": template_id,
                "params": ",".join(params),
            },
        )
        response.raise_for_status()
        return response.json()

    async def send_session_message(
        self, to: str, text: str, source: str,
        app_name: str | None = None,
    ) -> dict:
        """Send a free-form session message (within 24h window)."""
        response = await self._client.post(
            f"{self.config.base_url}/msg",
            headers={"apikey": self.config.api_key},
            data={
                "channel": "whatsapp",
                "source": source,
                "destination": f"91{to}",
                "src.name": app_name or self.config.app_name,
                "message": text,
            },
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._client.aclose()
```

### 3.5 msg91.py — SMS API Client

```python
import httpx
from dataclasses import dataclass

@dataclass
class MSG91Config:
    auth_key: str         # Platform-level MSG91 auth key
    base_url: str = "https://control.msg91.com/api/v5"


class MSG91Client:
    """SMS API client via MSG91. DLT-registered templates only."""

    def __init__(self, config: MSG91Config):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def send_sms(
        self, to: str, template_id: str, params: dict[str, str],
        sender_id: str,
    ) -> dict:
        """Send a DLT-registered SMS.

        Args:
            to: Recipient phone (normalized 10-digit)
            template_id: DLT template ID
            params: Template variable values {"var1": "value", "var2": "value"}
            sender_id: 6-char sender ID (e.g. "CITYGH")
        """
        response = await self._client.post(
            f"{self.config.base_url}/flow",
            headers={"authkey": self.config.auth_key},
            json={
                "template_id": template_id,
                "sender": sender_id,
                "recipients": [{"mobiles": f"91{to}", **params}],
            },
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._client.aclose()
```

### 3.6 truecaller.py — Truecaller Business Registration

```python
import httpx
from dataclasses import dataclass

@dataclass
class TruecallerConfig:
    partner_key: str      # Truecaller Business partner key
    base_url: str = "https://api.truecaller.com/v1/business"


class TruecallerClient:
    """Register hospital phone numbers with Truecaller for verified caller ID."""

    def __init__(self, config: TruecallerConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def register_numbers(
        self, numbers: list[str], business_name: str,
        category: str = "Hospital", logo_url: str | None = None,
    ) -> dict:
        """Register DIDs with Truecaller so calls show hospital name."""
        response = await self._client.post(
            f"{self.config.base_url}/register",
            headers={"Authorization": f"Bearer {self.config.partner_key}"},
            json={
                "phoneNumbers": numbers,
                "businessName": business_name,
                "category": category,
                "logoUrl": logo_url,
            },
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._client.aclose()
```

### 3.7 dlt.py — DLT Template Validation

```python
import re

DLT_TEMPLATE_PATTERN = re.compile(r"^\d{10,19}$")

def validate_dlt_template_id(template_id: str) -> bool:
    """Validate that a template ID matches TRAI DLT format."""
    return bool(DLT_TEMPLATE_PATTERN.match(template_id))
```

### 3.8 compliance.py — TCCCPR + NDNC

```python
from datetime import datetime, time
import pytz

IST = pytz.timezone("Asia/Kolkata")

TCCCPR_RULES = {
    "allowed_start": time(9, 0),    # 9:00 AM IST
    "allowed_end": time(21, 0),     # 9:00 PM IST
    "max_calls_per_day": 1,
    "max_calls_per_week": 3,
}


def is_within_allowed_hours(now: datetime | None = None) -> bool:
    """Check if current IST time is within TCCCPR allowed calling hours (9AM-9PM)."""
    if now is None:
        now = datetime.now(IST)
    elif now.tzinfo is None:
        now = IST.localize(now)
    else:
        now = now.astimezone(IST)

    current_time = now.time()
    return TCCCPR_RULES["allowed_start"] <= current_time <= TCCCPR_RULES["allowed_end"]
```

### 3.9 nmc_guardrails.py — NMC Advertising Content Checker

```python
import re
from dataclasses import dataclass

@dataclass
class NMCViolation:
    term: str
    rule: str
    severity: str     # "block" = cannot send, "warn" = flag for review
    suggestion: str

NMC_RULES = {
    "superlative_claims": {
        "severity": "block",
        "patterns": [
            r"\bbest\b", r"\b#1\b", r"\bnumber\s*one\b", r"\btop\s*(rated|ranked)\b",
            r"\bleading\b", r"\bmost\s+advanced\b", r"\bworld.?class\b",
        ],
        "suggestion": "Remove superlative. Use: 'experienced', 'qualified', 'specialized in'.",
    },
    "guaranteed_outcomes": {
        "severity": "block",
        "patterns": [
            r"\bguarantee[ds]?\b", r"\b100%\b", r"\bcure[ds]?\b",
            r"\bpermanent\s*(solution|cure|fix)\b", r"\brisk.?free\b",
        ],
        "suggestion": "Remove outcome guarantee. Use: 'treatment options', 'care plan'.",
    },
    "misleading_testimonials": {
        "severity": "block",
        "patterns": [
            r"\bpatient\s+says?\b.*\b(cured|healed|saved)\b",
            r"\btestimonial\b", r"\bsuccess\s+stor(y|ies)\b",
        ],
        "suggestion": "Patient testimonials with outcome claims are prohibited.",
    },
    "pricing_inducement": {
        "severity": "warn",
        "patterns": [
            r"\bfree\s+(surgery|treatment|consultation)\b",
            r"\bdiscount\b.*\b(surgery|treatment|procedure)\b",
        ],
        "suggestion": "Avoid price-based inducements for medical services.",
    },
}


def check_content(text: str) -> list[NMCViolation]:
    """Check text against NMC advertising rules.

    Returns empty list if compliant.
    Any violation with severity="block" means content MUST NOT be sent.
    """
    violations = []
    for rule_name, rule in NMC_RULES.items():
        for pattern in rule["patterns"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                violations.append(NMCViolation(
                    term=match.group(),
                    rule=rule_name,
                    severity=rule["severity"],
                    suggestion=rule["suggestion"],
                ))
    return violations


def has_blocking_violation(violations: list[NMCViolation]) -> bool:
    """Returns True if any violation has severity='block'."""
    return any(v.severity == "block" for v in violations)
```

---

## 4. Installation

Not published to PyPI. Installed directly from GitHub, **pinned to a git tag**:

```
# In requirements.txt of platform-api, queuecare, careloop:
spatiamed-common @ git+https://github.com/spatiamed/spatiamed-common.git@v0.1.0
```

**Always pin to a tag, never `@main`.** When updating the package, bump the tag, then update each service's requirements.txt one at a time. This prevents a breaking change in sm_common from hitting all services simultaneously.

For local development:

```bash
git clone https://github.com/spatiamed/spatiamed-common.git
cd spatiamed-common
pip install -e .
```

---

## 5. pyproject.toml

```toml
[project]
name = "spatiamed-common"
version = "0.1.0"
description = "Shared utilities for SpatiaMed platform services"
requires-python = ">=3.12"
dependencies = [
    "cryptography>=46.0",
    "httpx>=0.27",
    "pytz>=2024.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
]

[build-system]
requires = ["setuptools>=75.0"]
build-backend = "setuptools.backends._legacy:_Backend"
```

---

## 6. Which Services Use Which Modules

| Module | Platform API | QueueCare | CareLoop |
|--------|-------------|-----------|----------|
| phone.py | ✓ (hash staff email) | ✓ (hash patient phone) | ✓ (hash patient phone) |
| encryption.py | ✓ (staff PII) | ✓ (patient PII) | ✓ (patient PII) |
| webhook_auth.py | — | ✓ (sign + verify) | ✓ (sign + verify) |
| gupshup.py | — | ✓ (token notifications) | ✓ (campaigns, reminders) |
| msg91.py | — | ✓ (SMS fallback) | ✓ (SMS fallback) |
| truecaller.py | — | — | ✓ (caller ID registration) |
| dlt.py | — | ✓ (template validation) | ✓ (template validation) |
| compliance.py | — | — | ✓ (call timing) |
| nmc_guardrails.py | — | — | ✓ (content checking) |

---

## 7. Testing

Every module has unit tests. No integration tests (no external APIs in tests).

```bash
# Run all tests
cd spatiamed-common
pip install -e ".[dev]"
pytest tests/ -v

# Key test cases:
# phone.py:    normalize_phone handles all Indian formats, hash_phone is deterministic
# encryption.py: encrypt → decrypt round-trip, v1 prefix present, transport key round-trip
# webhook_auth.py: sign → verify round-trip, reject expired timestamps, reject tampered payloads
# nmc_guardrails.py: "best doctor" → blocked, "experienced doctor" → pass
# compliance.py: 9AM → allowed, 9:30PM → blocked
```

---

## 8. Success Criteria

| # | Criteria | Verification |
|---|---------|-------------|
| 1 | `hash_phone("+91 98765 43210", salt) == hash_phone("9876543210", salt)` | Unit test |
| 2 | encrypt_field → decrypt_field round-trip preserves plaintext | Unit test |
| 3 | sign_webhook → verify_webhook round-trip succeeds | Unit test |
| 4 | verify_webhook rejects payload older than 5 minutes | Unit test |
| 5 | verify_webhook rejects tampered signature | Unit test |
| 6 | NMC checker blocks "best doctor in India" | Unit test |
| 7 | NMC checker passes "experienced cardiologist" | Unit test |
| 8 | TCCCPR rejects calls at 9:30 PM IST | Unit test |
| 9 | Package installable via `pip install git+https://github.com/spatiamed/spatiamed-common.git@v0.1.0` | Install test |
| 10 | All three services import sm_common without version conflicts | Integration test |

---

## 9. Build Priority

Build only what's needed for the next service, not everything upfront:

| When Platform API starts (Phase 1) | Build: phone.py, encryption.py |
|-------------------------------------|-------------------------------|
| When QueueCare integrates (Phase 2) | Build: webhook_auth.py |
| When CareLoop starts (Phase 4) | Build: gupshup.py, msg91.py, truecaller.py, compliance.py, nmc_guardrails.py, dlt.py |

Don't build gupshup.py until CareLoop actually needs it. Ship the package incrementally.