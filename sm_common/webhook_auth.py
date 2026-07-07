import hashlib
import hmac
import json
import time
from typing import Any


def sign_webhook(payload: dict[str, Any], secret: str) -> tuple[str, int]:
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
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return signature, timestamp


def build_signed_request(payload: dict[str, Any], secret: str) -> tuple[dict[str, str], bytes]:
    """Sign a webhook payload for OUTBOUND delivery, returning (headers, body_bytes).

    Serializes the payload exactly ONCE and signs those exact bytes, then returns
    both the headers and the serialized body so the caller can POST the identical
    bytes it signed. Never re-serialize the payload separately (e.g. via
    ``httpx.post(json=payload)``) or the signature will not match the wire body.

    This is the canonical outbound counterpart to ``verify_webhook``:
      - X-Webhook-Signature: sha256=<hex>
      - X-Webhook-Timestamp: <unix-seconds>
      - signed message = f"{timestamp}.{body}" over the exact wire bytes

    Args:
        payload: The JSON-serializable webhook body.
        secret: WEBHOOK_SECRET (shared between sender and receiver).

    Returns:
        (headers, body_bytes) — send ``body_bytes`` as the request content with
        ``headers`` attached; the receiver's ``verify_webhook`` will validate.
    """
    timestamp = int(time.time())
    body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    message = f"{timestamp}.".encode() + body_bytes
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    headers = {
        "X-Webhook-Signature": f"sha256={signature}",
        "X-Webhook-Timestamp": str(timestamp),
        "Content-Type": "application/json",
    }
    return headers, body_bytes


def verify_webhook(
    body_bytes: bytes,
    signature_header: str,
    timestamp_header: str,
    secret: str,
    max_age_seconds: int = 300,
) -> bool:
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
    computed = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_sig, computed)
