"""Regex backstop for PHI in error events.

This is NOT the primary control. Names, MRNs and clinical free text cannot be
regex-redacted; the primary control is the identifiers-not-PHI contract (spec §6.5):
exception messages, log events and breadcrumbs carry tenant_id / patient_id /
phone_hash / record UUIDs, never raw PHI. This module catches mistakes.

Ported from CareLoop app/services/pii_sanitizer.py with fixes: digit boundaries,
Aadhaar before phone, E.164 + KSA, JWT/bearer, /t/<capability>, URL query strings,
and UUID protection (UUIDs are the identifiers we want to keep).
"""

from __future__ import annotations

import re

PHONE = "[PHONE]"
EMAIL = "[EMAIL]"
AADHAAR = "[AADHAAR]"
TOKEN = "[TOKEN]"
CAPABILITY = "/t/[REDACTED]"
QUERY = "?[Filtered]"

# Protected identifiers: UUIDs, and long hex tokens with at least one letter
# (event_id, trace_id, container ids, sha256 phone_hash). Without this, a bounded
# 10-digit run starting 6-9 inside a 32/64-hex id is "[PHONE]"-spliced: a few
# percent of event_ids would be corrupted (and GlitchTip drops a non-UUID event_id)
# and phone_hash (the sanctioned D5 identifier) mangled. The letter lookahead keeps
# an all-digit 12-digit Aadhaar unprotected.
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|(?<![0-9a-fA-F])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{12,64}(?![0-9a-fA-F])"
)

# Phase 1 runs on the WHOLE string: tokens, URLs, capabilities and emails must not
# be split by id protection (a capability or email local-part can look like hex).
_WHOLE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), TOKEN),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {TOKEN}"),
    (re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s#\"'<>]*"), rf"\1{QUERY}"),
    (re.compile(r"/t/[^/?#\s\"'<>]+"), CAPABILITY),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), EMAIL),
)
# Phase 2 (digit rules) runs only OUTSIDE protected ids. Aadhaar before phone.
_DIGIT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # (?<![\d+]): "+966512345678" / "+447911123456" are E.164 phones, not Aadhaar.
    (re.compile(r"(?<![\d+])[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"), AADHAAR),
    (re.compile(r"(?<![\d+])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), PHONE),
    (re.compile(r"(?<![\d+])\+[1-9]\d{7,14}(?!\d)"), PHONE),
    (re.compile(r"(?<!\d)05\d{8}(?!\d)"), PHONE),
)


def _digits(segment: str) -> str:
    for pattern, replacement in _DIGIT_RULES:
        segment = pattern.sub(replacement, segment)
    return segment


def redact_text(value: str) -> str:
    """Redact JWT/bearer tokens, URL query strings, /t/<capability>, emails, then
    Aadhaar and phones. Phase 2 skips protected ids (UUIDs, long hex)."""
    for pattern, replacement in _WHOLE_RULES:
        value = pattern.sub(replacement, value)
    out: list[str] = []
    last = 0
    for match in _UUID.finditer(value):
        out.append(_digits(value[last : match.start()]))
        out.append(match.group(0))
        last = match.end()
    out.append(_digits(value[last:]))
    return "".join(out)
