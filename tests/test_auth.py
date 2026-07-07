import time

import jwt
import pytest

from sm_common.auth import decode_jwt_with_grace

CURRENT = "current-secret"
PREVIOUS = "previous-secret"


def _make_token(secret: str, **claims: object) -> str:
    payload: dict[str, object] = {"sub": "u1", "iat": int(time.time())}
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


class TestDecodeJwtWithGrace:
    def test_current_key_ok(self):
        token = _make_token(CURRENT)
        payload = decode_jwt_with_grace(token, secret=CURRENT, previous_secret=PREVIOUS)
        assert payload["sub"] == "u1"

    def test_previous_key_ok_during_rotation(self):
        # Token signed with the OLD secret must still validate while previous is set.
        token = _make_token(PREVIOUS)
        payload = decode_jwt_with_grace(token, secret=CURRENT, previous_secret=PREVIOUS)
        assert payload["sub"] == "u1"

    def test_no_previous_key_rejects_old_token(self):
        token = _make_token(PREVIOUS)
        with pytest.raises(jwt.InvalidTokenError):
            decode_jwt_with_grace(token, secret=CURRENT, previous_secret=None)

    def test_both_fail_raises_original_error(self):
        token = _make_token("some-other-secret")
        with pytest.raises(jwt.InvalidSignatureError):
            decode_jwt_with_grace(token, secret=CURRENT, previous_secret=PREVIOUS)

    def test_malformed_token_raises(self):
        with pytest.raises(jwt.InvalidTokenError):
            decode_jwt_with_grace("not-a-jwt", secret=CURRENT, previous_secret=PREVIOUS)

    def test_expired_token_raises(self):
        token = _make_token(CURRENT, exp=int(time.time()) - 10)
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_jwt_with_grace(token, secret=CURRENT, previous_secret=PREVIOUS)

    def test_default_algorithm_is_hs256(self):
        token = _make_token(CURRENT)
        # No algorithms passed -> defaults to HS256, decodes fine.
        assert decode_jwt_with_grace(token, secret=CURRENT)["sub"] == "u1"

    def test_require_claim_enforced_on_primary_path(self):
        # require=[jti] is applied by jwt.decode; a validly-signed token missing jti
        # surfaces MissingRequiredClaimError (an InvalidTokenError) — validation is
        # NOT relaxed by the grace wrapper.
        token = _make_token(CURRENT)  # signed with current, but no jti claim
        with pytest.raises(jwt.MissingRequiredClaimError):
            decode_jwt_with_grace(
                token,
                secret=CURRENT,
                previous_secret=PREVIOUS,
                options={"require": ["jti"]},
            )

    def test_require_claim_not_relaxed_on_fallback(self):
        # Token signed with PREVIOUS but missing the required jti: the fallback path
        # must still reject it (raises InvalidTokenError), never silently accept.
        token = _make_token(PREVIOUS)  # no jti claim
        with pytest.raises(jwt.InvalidTokenError):
            decode_jwt_with_grace(
                token,
                secret=CURRENT,
                previous_secret=PREVIOUS,
                options={"require": ["jti"]},
            )

    def test_decode_kwargs_required_claim_present_passes(self):
        token = _make_token(PREVIOUS, jti="abc")
        payload = decode_jwt_with_grace(
            token,
            secret=CURRENT,
            previous_secret=PREVIOUS,
            options={"require": ["jti"]},
        )
        assert payload["jti"] == "abc"
