"""HTTP core for the Bahmni/OpenMRS adapter.

Basic auth on every call plus a cached ``JSESSIONID`` cookie (so an expired
cookie re-authenticates server-side), one re-login on a 401, and a fixed
mapping of HTTP failures onto the adapter exception types.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from sm_common.integrations.exceptions import (
    AuthError,
    ConfigError,
    TransientError,
    VendorRejected,
)

_SESSION_PATH = "/openmrs/ws/rest/v1/session"
_COOKIE = "JSESSIONID"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_epoch_ms(dt: datetime) -> int:
    """Aware datetime -> epoch milliseconds (int). Naive datetimes are refused."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("to_epoch_ms requires a timezone-aware datetime")
    delta = dt.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def from_epoch_ms(ms: int | float | str) -> datetime:
    """Epoch milliseconds (int/float/digit string) -> aware UTC datetime."""
    try:
        value = int(ms.strip()) if isinstance(ms, str) else int(ms)
        return _EPOCH + timedelta(milliseconds=value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"not an epoch-millisecond value: {ms!r}") from exc


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body["error"]["message"]
        if isinstance(msg, str) and msg:
            return msg
    except (ValueError, KeyError, TypeError):
        pass
    return resp.text[:300]


class BahmniHttp:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        ca_bundle_pem: str | None = None,
        verify_tls: bool = True,
        allow_insecure_tls: bool = False,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        verify: ssl.SSLContext | bool
        if ca_bundle_pem:
            verify = ssl.create_default_context(cadata=ca_bundle_pem)
        elif not verify_tls:
            if not allow_insecure_tls:
                raise ValueError(
                    "bahmni: verify_tls=False requires allow_insecure_tls=True (explicit opt-in)"
                )
            verify = False
        else:
            verify = True
        self._verify = verify
        self._auth = httpx.BasicAuth(username, password)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=verify,
            timeout=timeout_s,
            transport=transport,
        )
        self._cookie: str | None = None
        self._login_lock = asyncio.Lock()

    @property
    def verify(self) -> ssl.SSLContext | bool:
        return self._verify

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kw)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise TransientError(f"bahmni: transport error: {type(exc).__name__}") from exc
        except (httpx.UnsupportedProtocol, httpx.ProxyError) as exc:
            # A bad scheme or proxy setting never heals by retrying.
            raise ConfigError(f"bahmni: unusable connection config: {type(exc).__name__}") from exc

    async def _login(self, stale: str | None) -> str:
        """Establish a session; skip if another task already replaced the stale cookie."""
        async with self._login_lock:
            if self._cookie is not None and self._cookie != stale:
                return self._cookie
            self._client.cookies.clear()
            resp = await self._send("GET", _SESSION_PATH, auth=self._auth)
            if resp.status_code in (401, 403):
                raise AuthError("bahmni: login rejected")
            if resp.status_code >= 500:
                raise TransientError(f"bahmni: login HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise AuthError(f"bahmni: login HTTP {resp.status_code}")
            try:
                authenticated = bool(resp.json().get("authenticated"))
            except (ValueError, AttributeError):
                authenticated = False
            cookie = resp.cookies.get(_COOKIE)
            if not authenticated or not cookie:
                raise AuthError("bahmni: login failed (not authenticated)")
            self._cookie = cookie
            self._client.cookies.clear()
            return cookie

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        expect: tuple[int, ...] = (200, 201),
    ) -> httpx.Response:
        cookie = self._cookie or await self._login(None)
        relogged = False
        while True:
            resp = await self._send(
                method,
                path,
                params=params,
                json=json,
                auth=self._auth,
                headers={"Cookie": f"{_COOKIE}={cookie}"},
            )
            if resp.status_code == 401:
                if relogged:
                    raise AuthError("bahmni: HTTP 401 after re-login")
                cookie = await self._login(cookie)
                relogged = True
                continue
            break
        status = resp.status_code
        if status == 403:
            raise AuthError("bahmni: HTTP 403 forbidden")
        if status >= 500:
            raise TransientError(f"bahmni: HTTP {status}")
        if status in expect or status == 404:
            return resp
        if status == 400:
            raise VendorRejected(_error_message(resp))
        raise VendorRejected(f"bahmni: unexpected HTTP {status}: {_error_message(resp)}")
