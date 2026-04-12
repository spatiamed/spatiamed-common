import base64
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
