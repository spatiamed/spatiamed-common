"""Shared auth-header construction for HMS adapters.

Supports api_key, hmac, bearer, oauth2_client_credentials, private_key_jwt
(SMART Backend Services), and oauth2_password (OpenEMR Standard API). OAuth
tokens are cached with a monotonic expiry: in the caller-owned TokenCache when
the cfg carries one (``cfg["_token_cache"]`` + ``cfg["_token_slot"]``, wired by
build_adapter from AdapterBuildConfig.token_cache), otherwise on the passed cfg
dict under the private key "_oauth_cache".

A cfg dict lives only as long as its adapter, and adapters are typically built
per poll cycle, so the cfg-dict cache alone means a token per cycle. A caller
that wants tokens to outlive the adapter passes a TokenCache it keeps (keyed by
whatever identifies the integration on its side; sm_common never shares tokens
between cfgs on its own).

private_key_jwt is what FHIR servers require before they will issue system-level
scopes: the client proves itself with an RS384-signed assertion validated against
a registered jwks_uri, never a shared secret.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from email.utils import formatdate
from typing import Any, Protocol, runtime_checkable

import httpx
import jwt

from sm_common.integrations.exceptions import AuthError, TransientError


# Refresh this long before the vendor's expiry, so a token is never sent in its
# last seconds and refused mid-request.
EXPIRY_MARGIN_SECONDS = 30


@runtime_checkable
class TokenCache(Protocol):
    """Caller-owned OAuth token store that outlives an adapter.

    ``slot`` names the identity a token belongs to within one integration
    ("system", or "write_user" for OpenEMR's password-grant user). ``expires_at``
    is a ``time.monotonic()`` instant, so a cache must stay in-process: never
    persist it.

    ``set`` runs as soon as a token is minted and before it is first sent, which
    makes it the place for a caller to register the token with its log
    scrubber.
    """

    def get(self, slot: str) -> tuple[str, float] | None: ...

    def set(self, slot: str, token: str, expires_at: float) -> None: ...

    def invalidate(self, slot: str) -> None: ...


class InMemoryTokenCache:
    """The simplest TokenCache: a dict per instance. One instance per integration."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, slot: str) -> tuple[str, float] | None:
        return self._tokens.get(slot)

    def set(self, slot: str, token: str, expires_at: float) -> None:
        self._tokens[slot] = (token, expires_at)

    def invalidate(self, slot: str) -> None:
        self._tokens.pop(slot, None)


def _external_cache(cfg: dict) -> tuple[TokenCache, str] | None:  # type: ignore[type-arg]
    cache = cfg.get("_token_cache")
    if cache is None:
        return None
    return cache, str(cfg.get("_token_slot") or "system")


def _cached_token(cfg: dict, now: float) -> str | None:  # type: ignore[type-arg]
    ext = _external_cache(cfg)
    if ext is not None:
        cache, slot = ext
        hit = cache.get(slot)
        if hit is not None and hit[1] > now + EXPIRY_MARGIN_SECONDS:
            return str(hit[0])
        return None
    local = cfg.get("_oauth_cache")
    if local and local["expires_at"] > now + EXPIRY_MARGIN_SECONDS:
        return str(local["token"])
    return None


def _store_token(cfg: dict, payload: dict[str, Any], now: float) -> str:  # type: ignore[type-arg]
    token = str(payload["access_token"])
    expires_at = now + int(payload.get("expires_in", 3600))
    ext = _external_cache(cfg)
    if ext is not None:
        cache, slot = ext
        cache.set(slot, token, expires_at)
    else:
        cfg["_oauth_cache"] = {"token": token, "expires_at": expires_at}
    return token


async def build_auth_headers(
    client: httpx.AsyncClient, scheme: str, cfg: dict, body: str = ""
) -> dict[str, str]:
    base = {"Content-Type": "application/json"}
    if scheme == "api_key":
        header = cfg.get("api_key_header", "X-Api-Key")
        return {**base, header: cfg.get("api_key", "")}
    if scheme == "bearer":
        token = (cfg.get("bearer_token") or "").strip()
        if not token:
            # "Bearer " is an illegal header value and httpx refuses to send it,
            # so an unset token crashed the transport instead of surfacing as a
            # credentials problem. AuthError is what the poll worker already
            # handles: it records health_status="auth_error" and stops retrying.
            raise AuthError("bearer auth selected but no bearer_token configured")
        return {**base, "Authorization": f"Bearer {token}"}
    if scheme == "hmac":
        date_str = formatdate(usegmt=True)
        secret = cfg.get("api_secret", "")
        sig = hmac.new(secret.encode(), f"{date_str}\n{body}".encode(), hashlib.sha256).hexdigest()
        header = cfg.get("api_key_header", "X-Api-Key")
        return {**base, header: cfg.get("api_key", ""), "X-Signature": sig, "Date": date_str}
    if scheme == "oauth2_client_credentials":
        token = await _oauth_token(client, cfg)
        return {**base, "Authorization": f"Bearer {token}"}
    if scheme == "private_key_jwt":
        token = await _private_key_jwt_token(client, cfg)
        return {**base, "Authorization": f"Bearer {token}"}
    if scheme == "oauth2_password":
        token = await _password_grant_token(client, cfg)
        return {**base, "Authorization": f"Bearer {token}"}
    return base


async def _post_token(
    client: httpx.AsyncClient, token_url: str, data: dict, grant: str
) -> httpx.Response:
    """POST to a token endpoint, turning failures into typed adapter errors.

    A refused credential (400/401/403) is AuthError: retrying will not help and
    the admin needs to see it. An unreachable endpoint, a timeout or a 5xx is
    TransientError. A raw httpx error used to escape from here, and callers
    that only catch adapter errors turned it into a bare 500.
    """
    try:
        resp = await client.post(token_url, data=data)
    except httpx.TransportError as exc:
        raise TransientError(f"{grant} token endpoint unreachable: {exc!r}") from exc
    if resp.status_code in (400, 401, 403):
        raise AuthError(f"{grant} refused: HTTP {resp.status_code} {resp.text[:200]}")
    if resp.status_code >= 500:
        raise TransientError(f"{grant} token endpoint failed: HTTP {resp.status_code}")
    if not resp.is_success:
        # 429, a redirect, a 404 from a mistyped token_url, ... used to escape as
        # a raw httpx.HTTPStatusError, which no caller classifies. None of them is
        # a refused credential, so they are retryable rather than auth failures.
        raise TransientError(f"{grant} token endpoint returned HTTP {resp.status_code}")
    return resp


def _token_payload(resp: httpx.Response, grant: str) -> dict[str, Any]:
    """Parse a 2xx token response, turning a malformed body into TransientError.

    A token endpoint that answers 2xx with an HTML error page or an empty body
    (a proxy, a broken PHP backend, a database outage behind the HMS) made
    ``resp.json()`` raise a bare JSONDecodeError. Callers that only catch the
    typed adapter errors logged it as an unexpected crash with a stack trace,
    while others treated it as transient. It is the server misbehaving, not our
    credential being refused, so it is TransientError everywhere. The body is
    not echoed: it is the vendor's, and the message lands in logs and the DB.
    """
    try:
        payload = resp.json()
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        ctype = resp.headers.get("content-type", "?")
        raise TransientError(
            f"{grant} token endpoint returned a non-JSON HTTP {resp.status_code} body ({ctype})"
        ) from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise TransientError(
            f"{grant} token endpoint returned HTTP {resp.status_code} without an access_token"
        )
    return payload


async def _oauth_token(client: httpx.AsyncClient, cfg: dict) -> str:
    now = time.monotonic()
    cached = _cached_token(cfg, now)
    if cached is not None:
        return cached
    data = {
        "grant_type": "client_credentials",
        "client_id": cfg.get("client_id", ""),
        "client_secret": cfg.get("client_secret", ""),
    }
    if cfg.get("scopes"):
        data["scope"] = cfg["scopes"]
    resp = await _post_token(client, cfg["token_url"], data, "client_credentials grant")
    payload = _token_payload(resp, "client_credentials grant")
    return _store_token(cfg, payload, now)


def _client_assertion(cfg: dict) -> str:
    """Sign a one-shot client assertion for the token endpoint.

    ``aud`` must be the token URL and ``jti`` must be unique — servers reject a
    replayed assertion, so a cached one is worse than useless.
    """
    now = int(time.time())
    token_url = cfg["token_url"]
    client_id = cfg.get("client_id", "")
    headers = {}
    if cfg.get("kid"):
        headers["kid"] = cfg["kid"]
    return jwt.encode(
        {
            "iss": client_id,
            "sub": client_id,
            "aud": token_url,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + int(cfg.get("assertion_ttl_seconds", 300)),
        },
        cfg["private_key_pem"],
        algorithm=cfg.get("assertion_alg", "RS384"),
        headers=headers or None,
    )


async def _private_key_jwt_token(client: httpx.AsyncClient, cfg: dict) -> str:
    now = time.monotonic()
    cached = _cached_token(cfg, now)
    if cached is not None:
        return cached

    data = {
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": _client_assertion(cfg),
    }
    if cfg.get("scopes"):
        data["scope"] = cfg["scopes"]

    resp = await _post_token(client, cfg["token_url"], data, "private_key_jwt grant")
    payload = _token_payload(resp, "private_key_jwt grant")
    return _store_token(cfg, payload, now)


def invalidate_token(cfg: dict) -> None:
    """Forget a cached token — call after the vendor answers 401 with it."""
    cfg.pop("_oauth_cache", None)
    ext = _external_cache(cfg)
    if ext is not None:
        cache, slot = ext
        cache.invalidate(slot)


async def _password_grant_token(client: httpx.AsyncClient, cfg: dict) -> str:
    """OAuth2 password grant with OpenEMR's user_role parameter.

    OpenEMR-specific: its Standard API (the only place appointments can be
    created) refuses system tokens — "the api route is only for users role".
    The password grant is OFF by default in OpenEMR (oauth_password_grant);
    the hospital must enable it and issue a dedicated service account.
    """
    now = time.monotonic()
    cached = _cached_token(cfg, now)
    if cached is not None:
        return cached
    data = {
        "grant_type": "password",
        "client_id": cfg.get("client_id", ""),
        "client_secret": cfg.get("client_secret", ""),
        "username": cfg.get("username", ""),
        "password": cfg.get("password", ""),
        "user_role": cfg.get("user_role", "users"),
    }
    if cfg.get("scopes"):
        data["scope"] = cfg["scopes"]
    resp = await _post_token(client, cfg["token_url"], data, "password grant")
    payload = _token_payload(resp, "password grant")
    return _store_token(cfg, payload, now)
