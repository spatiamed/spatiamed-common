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
