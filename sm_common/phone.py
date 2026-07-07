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


def hash_normalized(normalized: str, salt: str) -> str:
    """Low-level salted SHA-256 primitive over an already-normalized identifier.

    This is the single canonical hashing scheme for cross-system patient identity:
    ``sha256(f"{salt}{normalized}").hexdigest()``. Both the India-only
    :func:`hash_phone` and the international :func:`hash_phone_e164` delegate here so
    the salt/algorithm can never drift between the domestic and international paths.

    Callers that already hold a normalized value (E.164 digits, or any other
    caller-normalized identifier in its own disjoint input space) should use this
    directly rather than re-implementing the SHA-256 by hand.

    Args:
        normalized: The already-normalized identifier to hash. NOT re-validated here.
        salt: HASH_SALT (must be identical across all services sharing the join space).

    Returns:
        64-char hex digest.
    """
    return hashlib.sha256(f"{salt}{normalized}".encode()).hexdigest()


def hash_phone(phone: str, salt: str) -> str:
    """Deterministic salted SHA-256 hash for cross-system patient identity.

    Args:
        phone: Raw phone number (any format — will be normalized)
        salt: HASH_SALT environment variable (must be identical across all services)

    Returns:
        64-char hex digest. Identical output for the same phone+salt in any service.
    """
    return hash_normalized(normalize_phone(phone), salt)


def normalize_e164(raw: str) -> str:
    """Lenient international normalization for non-Indian callers.

    Keeps the country-code digits (drops a leading ``+`` or ``00`` prefix). Used ONLY
    on international inbound paths — :func:`normalize_phone` is India-only and rejects
    these numbers. International caller hashes live in their own input space, disjoint
    from the India-only CareLoop<->QueueCare join space, so they never collide with a
    domestic hash.

    Returns: the country-code-prefixed digit string (e.g. "14155550123").
    Raises: ValueError if fewer than 8 digits remain.
    """
    s = (raw or "").strip()
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 8:
        raise ValueError(f"Invalid international number: {raw!r}")
    return digits


def hash_phone_e164(phone: str, salt: str) -> str:
    """International caller hash — same salted-SHA-256 scheme as :func:`hash_phone` but
    over the E.164-normalized number, bypassing the India-only validation.

    Distinct input space (see :func:`normalize_e164`) means the output never collides
    with a domestic :func:`hash_phone` value.

    Args:
        phone: Raw international phone number (any format — normalized via normalize_e164).
        salt: HASH_SALT (the SAME salt used for hash_phone).

    Returns:
        64-char hex digest, byte-identical to ``sha256(f"{salt}{normalize_e164(phone)}")``.
    """
    return hash_normalized(normalize_e164(phone), salt)
