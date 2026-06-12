import base64
import hashlib
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --- Field-level encryption (for storage) ---


def encrypt_field(plaintext: str, key: bytes) -> str:
    """AES-256-GCM encrypt a PII field for database storage.

    Args:
        plaintext: The value to encrypt (name, email, phone)
        key: 32-byte encryption key (service-specific ENCRYPTION_KEY)

    Returns:
        Base64-encoded string: "v1:<nonce+ciphertext>"
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

    NOTE (2026-06-12): The CareLoop→QueueCare webhook transport flow
    (WEBHOOK_TRANSPORT_KEY) is the intended consumer of these two functions.
    That flow currently implements Fernet directly in QueueCare's
    careloop_handlers.py rather than calling encrypt_for_transport /
    decrypt_from_transport. If that flow is ever refactored to call these
    helpers, the direct Fernet usage in careloop_handlers.py should be removed.

    Args:
        plaintext: PII value to send
        transport_key: WEBHOOK_TRANSPORT_KEY (shared Fernet key)
    """
    f = Fernet(transport_key.encode())
    return f.encrypt(plaintext.encode()).decode()


def decrypt_from_transport(ciphertext: str, transport_key: str) -> str:
    """Decrypt PII received via webhook. Then re-encrypt with local storage key.

    See encrypt_for_transport docstring for the current consumer status note.
    """
    f = Fernet(transport_key.encode())
    return f.decrypt(ciphertext.encode()).decode()


# --- FieldEncryptor: OOP key-versioned AES-256-GCM (for services that need rotation) ---
#
# IMPORTANT — v1: prefix collision:
#   encrypt_field() above also uses "v1:" but its format is "v1:{base64(nonce+ct)}" (AES-GCM).
#   FieldEncryptor treats "v1:" as a legacy Fernet ciphertext (PBKDF2-derived key).
#   These two are NOT interchangeable. Do NOT attempt to decrypt an encrypt_field() ciphertext
#   with FieldEncryptor.decrypt() or vice versa.


class FieldEncryptor:
    """AES-256-GCM field encryption with key versioning, legacy Fernet migration, and lookup hashing.

    Ciphertext format: v{version}:{nonce_b64}:{ciphertext_b64}

    The legacy "v1:" prefix refers to old Fernet-encrypted data (pre-migration). This is
    intentionally different from sm_common.encryption.encrypt_field() which also uses "v1:"
    but for a different AES-GCM wire format — see module-level note above.
    """

    def __init__(
        self,
        master_key_hex: str,
        hash_salt: str,
        key_version: int = 2,
        *,
        legacy_master_key: str | None = None,
    ) -> None:
        key_bytes = bytes.fromhex(master_key_hex)
        if len(key_bytes) != 32:
            raise ValueError(f"Encryption key must be 256 bits (32 bytes), got {len(key_bytes)}")
        self._aesgcm = AESGCM(key_bytes)
        self._key_version = key_version
        self._hash_salt = hash_salt

        self._legacy_fernet = None
        if legacy_master_key:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"queuecare-v1",
                iterations=480_000,
            )
            fernet_key = base64.urlsafe_b64encode(kdf.derive(legacy_master_key.encode()))
            self._legacy_fernet = Fernet(fernet_key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt with AES-256-GCM. Returns v{version}:{nonce_b64}:{ciphertext_b64}."""
        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode(), None)
        nonce_b64 = base64.b64encode(nonce).decode()
        ct_b64 = base64.b64encode(ciphertext).decode()
        return f"v{self._key_version}:{nonce_b64}:{ct_b64}"

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt AES-256-GCM (v2+) or legacy Fernet (v1:) ciphertext."""
        if ciphertext.startswith("v1:"):
            return self._decrypt_legacy_fernet(ciphertext[3:])

        parts = ciphertext.split(":", 2)
        if len(parts) != 3 or not parts[0].startswith("v"):
            raise ValueError("Invalid ciphertext format")

        nonce = base64.b64decode(parts[1])
        ct = base64.b64decode(parts[2])
        return self._aesgcm.decrypt(nonce, ct, None).decode()

    def _decrypt_legacy_fernet(self, raw_ciphertext: str) -> str:
        if self._legacy_fernet is None:
            raise ValueError("Cannot decrypt v1: Fernet ciphertext — no legacy_master_key provided")
        return self._legacy_fernet.decrypt(raw_ciphertext.encode()).decode()

    def needs_reencrypt(self, ciphertext: str) -> bool:
        """Return True if ciphertext uses an older format that should be migrated."""
        if ciphertext.startswith("v1:"):
            return True
        parts = ciphertext.split(":", 2)
        if len(parts) >= 1 and parts[0].startswith("v"):
            try:
                return int(parts[0][1:]) < self._key_version
            except ValueError:
                pass
        return False

    def reencrypt(self, ciphertext: str) -> str:
        """Decrypt and re-encrypt with the current key version."""
        return self.encrypt(self.decrypt(ciphertext))

    def hash_for_lookup(self, value: str) -> str:
        """Deterministic salted SHA-256 hash for phone/email lookup."""
        salted = self._hash_salt + value.strip().lower()
        return hashlib.sha256(salted.encode()).hexdigest()
