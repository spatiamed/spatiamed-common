import json
import time
from unittest.mock import patch

import pytest
from sm_common.webhook_auth import sign_webhook, verify_webhook


class TestSignWebhook:
    def test_returns_signature_and_timestamp(self):
        sig, ts = sign_webhook({"event": "test"}, "secret")
        assert isinstance(sig, str)
        assert len(sig) == 64
        assert isinstance(ts, int)

    def test_deterministic_for_same_timestamp(self):
        with patch("sm_common.webhook_auth.time") as mock_time:
            mock_time.time.return_value = 1000000
            sig1, ts1 = sign_webhook({"a": 1}, "secret")
            sig2, ts2 = sign_webhook({"a": 1}, "secret")
        assert sig1 == sig2
        assert ts1 == ts2

    def test_different_payload_different_sig(self):
        with patch("sm_common.webhook_auth.time") as mock_time:
            mock_time.time.return_value = 1000000
            sig1, _ = sign_webhook({"a": 1}, "secret")
            sig2, _ = sign_webhook({"a": 2}, "secret")
        assert sig1 != sig2


class TestVerifyWebhook:
    def test_sign_verify_round_trip(self):
        payload = {"event": "patient.created", "id": "123"}
        secret = "test-webhook-secret"
        sig, ts = sign_webhook(payload, secret)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        assert verify_webhook(body, f"sha256={sig}", str(ts), secret)

    def test_wrong_secret_fails(self):
        payload = {"event": "test"}
        sig, ts = sign_webhook(payload, "correct-secret")
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        assert not verify_webhook(body, f"sha256={sig}", str(ts), "wrong-secret")

    def test_tampered_body_fails(self):
        payload = {"event": "test"}
        sig, ts = sign_webhook(payload, "secret")
        tampered = b'{"event":"hacked"}'
        assert not verify_webhook(tampered, f"sha256={sig}", str(ts), "secret")

    def test_expired_timestamp_fails(self):
        payload = {"event": "test"}
        secret = "secret"
        old_ts = int(time.time()) - 600  # 10 minutes ago
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        message = f"{old_ts}.{body}"
        import hashlib, hmac as hmac_mod
        sig = hmac_mod.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        assert not verify_webhook(body.encode(), f"sha256={sig}", str(old_ts), secret)

    def test_invalid_timestamp_returns_false(self):
        assert not verify_webhook(b"body", "sha256=abc", "not-a-number", "secret")

    def test_none_timestamp_returns_false(self):
        assert not verify_webhook(b"body", "sha256=abc", None, "secret")

    def test_without_sha256_prefix(self):
        """verify_webhook should handle signature without sha256= prefix."""
        payload = {"event": "test"}
        secret = "secret"
        sig, ts = sign_webhook(payload, secret)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        # Pass raw sig without prefix - removeprefix("sha256=") is a no-op
        assert verify_webhook(body, sig, str(ts), secret)
