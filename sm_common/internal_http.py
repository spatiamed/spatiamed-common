"""Service-to-service HTTP client with the ``X-Internal-Secret`` auth contract.

Every SpatiaMed backend authenticates internal (service-to-service) calls with a
single shared header::

    X-Internal-Secret: <secret>

The server verifies it with ``hmac.compare_digest`` (see
``sm_common.fastapi_guard.verify_internal_secret``). This module is the client
side of that contract — a thin, un-opinionated wrapper over ``httpx.AsyncClient``
that injects the header on every request. It deliberately does NOT bake in
retries or caching; callers own those.

Usage — one-shot (context manager, closes the transport on exit)::

    async with InternalClient(base_url, secret) as client:
        resp = await client.get("/internal/foo", params={"x": 1})
        resp.raise_for_status()

Usage — persistent (reuse one client for the process, close on shutdown)::

    client = InternalClient(base_url, secret)
    ...
    resp = await client.post("/internal/bar", json={"a": 1})
    ...
    await client.aclose()
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

INTERNAL_SECRET_HEADER = "X-Internal-Secret"


class InternalClient:
    """An ``httpx.AsyncClient``-backed client that injects ``X-Internal-Secret``.

    Args:
        base_url: Base URL of the target service (e.g. ``http://queuecare:8000``).
        secret: The shared internal secret. Sent verbatim on every request.
        timeout: Per-request timeout in seconds (default 10.0).

    The injected ``X-Internal-Secret`` header always wins over any caller-supplied
    header of the same name, so the auth contract cannot be accidentally clobbered.
    """

    def __init__(self, base_url: str, secret: str, timeout: float = 10.0) -> None:
        self._secret = secret
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    def _merge_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        merged: dict[str, str] = dict(headers) if headers else {}
        merged[INTERNAL_SECRET_HEADER] = self._secret
        return merged

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send an arbitrary request with the internal-secret header injected."""
        return await self._client.request(
            method, path, headers=self._merge_headers(headers), **kwargs
        )

    async def get(
        self, path: str, *, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> httpx.Response:
        return await self.request("GET", path, headers=headers, **kwargs)

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        content: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        return await self.request(
            "POST", path, json=json, content=content, headers=headers, **kwargs
        )

    async def aclose(self) -> None:
        """Close the underlying transport. Safe to call more than once."""
        await self._client.aclose()

    async def __aenter__(self) -> InternalClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
