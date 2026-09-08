"""Shared auth-header construction for HMS adapters.

Supports api_key, hmac, bearer, oauth2_client_credentials, and private_key_jwt
(SMART Backend Services). OAuth tokens are cached on the passed cfg dict under
the private key "_oauth_cache" with a monotonic expiry.

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

import httpx
import jwt


async def build_auth_headers(
    client: httpx.AsyncClient, scheme: str, cfg: dict, body: str = ""
) -> dict[str, str]:
    base = {"Content-Type": "application/json"}
    if scheme == "api_key":
        header = cfg.get("api_key_header", "X-Api-Key")
        return {**base, header: cfg.get("api_key", "")}
    if scheme == "bearer":
        return {**base, "Authorization": f"Bearer {cfg.get('bearer_token', '')}"}
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
    return base


async def _oauth_token(client: httpx.AsyncClient, cfg: dict) -> str:
    cache = cfg.get("_oauth_cache")
    now = time.monotonic()
    if cache and cache["expires_at"] > now + 30:
        return cache["token"]
    data = {
        "grant_type": "client_credentials",
        "client_id": cfg.get("client_id", ""),
        "client_secret": cfg.get("client_secret", ""),
    }
    if cfg.get("scopes"):
        data["scope"] = cfg["scopes"]
    resp = await client.post(cfg["token_url"], data=data)
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    cfg["_oauth_cache"] = {"token": token, "expires_at": now + int(payload.get("expires_in", 3600))}
    return token


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
    cache = cfg.get("_oauth_cache")
    now = time.monotonic()
    if cache and cache["expires_at"] > now + 30:
        return str(cache["token"])

    data = {
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": _client_assertion(cfg),
    }
    if cfg.get("scopes"):
        data["scope"] = cfg["scopes"]

    resp = await client.post(cfg["token_url"], data=data)
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    cfg["_oauth_cache"] = {"token": token, "expires_at": now + int(payload.get("expires_in", 3600))}
    return str(token)
