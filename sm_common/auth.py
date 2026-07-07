"""JWT decode helpers shared across SpatiaMed services.

Pure PyJWT — this module intentionally does NOT import FastAPI so that every
service (whether or not it uses FastAPI at the call site) can share the exact same
secret-rotation grace-window semantics. Callers translate the raised
``jwt.InvalidTokenError`` into their own HTTP/transport error.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import jwt

__all__ = ["decode_jwt_with_grace"]

_DEFAULT_ALGORITHMS = ("HS256",)


def decode_jwt_with_grace(
    token: str,
    *,
    secret: str,
    previous_secret: str | None = None,
    algorithms: Sequence[str] | None = None,
    **decode_kwargs: Any,
) -> dict[str, Any]:
    """Decode a JWT, tolerating an in-flight secret rotation.

    Tries ``secret`` first. If that raises :class:`jwt.InvalidTokenError` (bad
    signature, expired, missing required claim, etc.) AND ``previous_secret`` is
    configured, retries the decode with ``previous_secret``. This is the grace
    window during which tokens signed with the just-rotated previous secret are
    still accepted, up to their TTL.

    All validation semantics are those of :func:`jwt.decode`: the same
    ``algorithms`` and any ``options`` / ``leeway`` / ``require`` passed through
    ``decode_kwargs`` apply identically to BOTH the primary and previous-secret
    attempts. This helper never relaxes validation — it only widens the accepted
    signing key set during a rotation.

    Args:
        token: The encoded JWT.
        secret: The current (primary) signing secret.
        previous_secret: The prior signing secret to accept during the rotation
            grace window, or ``None`` to disable the fallback (single-key behaviour,
            byte-identical to a plain ``jwt.decode``).
        algorithms: Permitted signature algorithms. Defaults to ``["HS256"]``.
        **decode_kwargs: Forwarded verbatim to :func:`jwt.decode` (e.g.
            ``options={"require": [...]}``, ``leeway=30``, ``audience=...``).

    Returns:
        The decoded claims payload.

    Raises:
        jwt.InvalidTokenError: If decoding fails under ``secret`` and either no
            ``previous_secret`` is configured or the previous-secret retry also
            fails. When both keys fail, the ORIGINAL (primary-secret) error is
            raised, since the current secret is the expected signer.
    """
    algs = list(_DEFAULT_ALGORITHMS if algorithms is None else algorithms)
    try:
        return jwt.decode(token, secret, algorithms=algs, **decode_kwargs)
    except jwt.InvalidTokenError as primary_exc:
        if previous_secret is None:
            raise
        try:
            return jwt.decode(token, previous_secret, algorithms=algs, **decode_kwargs)
        except jwt.InvalidTokenError:
            raise primary_exc from None
