"""Shared auth-header construction for HMS adapters.

Supports api_key, hmac, bearer, and oauth2_client_credentials (SMART-on-FHIR
client-credentials). OAuth tokens are cached on the passed cfg dict under
the private key "_oauth_cache" with a monotonic expiry.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from email.utils import formatdate

import httpx


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
