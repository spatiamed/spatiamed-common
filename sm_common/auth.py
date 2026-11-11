"""JWT decode helpers shared across SpatiaMed services.

Pure PyJWT — this module intentionally does NOT import FastAPI so that every
service (whether or not it uses FastAPI at the call site) can share the exact same
secret-rotation grace-window semantics. Callers translate the raised
``jwt.InvalidTokenError`` into their own HTTP/transport error.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import jwt

__all__ = [
    "ROLES_EPOCH_KEY_PREFIX",
    "decode_jwt_with_grace",
    "has_any_role",
    "roles_epoch_key",
    "roles_from_claims",
    "roles_revoked",
]

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


# --- multi-role claims (staff module access) --------------------------------
# platform-api mints ``roles`` (primary ``platform_role`` first, then extra module
# roles) next to the legacy single ``platform_role`` claim. Consumers must accept
# BOTH shapes: tokens minted before the change carry only ``platform_role``.

ROLES_EPOCH_KEY_PREFIX = "staff_roles_epoch:"


def roles_from_claims(payload: Mapping[str, Any]) -> frozenset[str]:
    """Roles a decoded token holds.

    Uses the ``roles`` claim when it is a non-empty list; non-string / blank items
    are ignored. A missing, empty, or wholly malformed ``roles`` falls back to the
    legacy ``platform_role`` claim, so old tokens behave exactly as before. The
    legacy primary role is always included when present (fail toward today's
    behaviour, never toward less). Returns an empty set when neither is usable.
    """
    found: set[str] = set()
    raw = payload.get("roles")
    if isinstance(raw, (list, tuple)):
        found.update(r for r in raw if isinstance(r, str) and r)
    primary = payload.get("platform_role")
    if isinstance(primary, str) and primary:
        found.add(primary)
    return frozenset(found)


def has_any_role(roles: Iterable[str], allowed: Iterable[str]) -> bool:
    """True when ``roles`` and ``allowed`` share at least one role."""
    return not frozenset(roles).isdisjoint(frozenset(allowed))


def roles_epoch_key(staff_id: str) -> str:
    """Redis key platform-api writes (unix seconds) when it revokes a staff member's
    roles. Tokens issued before that instant must stop working immediately."""
    return f"{ROLES_EPOCH_KEY_PREFIX}{staff_id}"


def roles_revoked(iat: Any, epoch: Any) -> bool:
    """True when a token issued at ``iat`` predates the staff member's role epoch.

    ``epoch`` is the raw Redis value (bytes / str / int / None). ``None`` means no
    revocation recorded -> False. A value that is present but NOT an integer is
    treated as revoked (fail closed: a corrupt revoke marker must never let an old
    token through). A token whose ``iat`` is missing or unparseable while an epoch
    exists is also treated as revoked (cannot prove it postdates the revoke).
    Strict ``<``: a token minted in the same second as the revoke is kept
    (platform-api revokes first, the re-minted token follows).
    """
    if epoch is None:
        return False
    try:
        if isinstance(epoch, (bytes, bytearray)):
            epoch = epoch.decode()
        epoch_i = int(epoch)
    except (ValueError, TypeError):
        return True
    try:
        iat_i = int(iat)
    except (ValueError, TypeError):
        return True
    return iat_i < epoch_i
