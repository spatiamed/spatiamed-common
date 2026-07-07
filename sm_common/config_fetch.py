"""Resolve tenant config from platform-api (HTTP) with a Redis cache + 7-day LKG fallback.

This is the single, hardened implementation of a pattern that had been hand-copied
into four consumer modules (QueueCare journey/coding clients, QueueCare
notification_service CareLoop client, CareLoop journey client) and had already
diverged in unsafe ways. See spatiamed-common#9 (audit XR-07).

Resolution chain (``resolve_config``), each tier tried in order, and NEVER raising:

    1. fresh Redis cache          -> parsed as source="live"
    2. live platform-api fetch    -> writes fresh + LKG cache, parsed as source="live"
    3. last-known-good Redis cache -> parsed as source="last_known_good"
    4. caller-supplied fallback   -> ``local_fallback()``  (source is whatever the caller built)

A slow or down platform-api must NEVER break the request: every failure degrades to
the next tier and is logged.

Fail-SAFE, caller-controlled fallback (the compliance-critical property)
------------------------------------------------------------------------
When BOTH the HTTP fetch and every cache tier fail, this helper returns exactly what
the caller passed as ``local_fallback`` — it bakes in **no domain default of its own**.

This matters for patient-safety-adjacent config. The former clinical-coding hand-copy
defaulted to ``medical_system="allopathy"`` + ``dual_coding_required=False`` on a
platform-api outage, so a hospital that legally requires AYUSH/NAMASTE dual-coding
would silently lose that requirement during an outage. With this API the caller owns
that decision: it passes its own *fail-closed* fallback (e.g. one that keeps
dual-coding required, or that raises/alerts), and the library will never override it
with a permissive default.

Hardening guarantees (fixes for the four divergent copies):
  * Guarded cache read — a Redis error, missing key, unparseable value, or malformed
    body shape is a cache miss, never an exception (fixes the notification_service
    fail-unsafe ``json.loads`` and the parse errors).
  * Guarded TTL policy — a dynamic ``ttl`` callable that raises (e.g. a body whose
    ``ttl_tiers`` is not a dict, so ``.values()`` ``AttributeError``s) falls back to
    ``DEFAULT_FRESH_TTL_SECONDS`` instead of propagating.
  * Single 7-day LKG constant (``LKG_TTL_SECONDS``) and a single internal-call timeout
    default (``DEFAULT_INTERNAL_TIMEOUT_SECONDS``); the copies drifted 2s/10s/15s/30s.
    The caller may still override the timeout per-spec.
  * Fully typed and generic over the parsed payload type ``T``.

Usage::

    from dataclasses import dataclass
    from sm_common.config_fetch import ConfigSpec, resolve_config, static_fallback

    @dataclass(frozen=True)
    class Coding:
        profile: str
        dual_coding_required: bool
        source: str

    def _valid(body: object) -> bool:
        return isinstance(body, dict) and isinstance(body.get("coding_config"), dict)

    async def _parse(body, source):  # source: "live" | "last_known_good"
        cc = body["coding_config"]
        return Coding(cc["profile"], bool(cc.get("dual_coding_required", False)), source)

    spec = ConfigSpec[Coding](
        base_url=settings.PLATFORM_API_INTERNAL_URL,
        secret=settings.INTERNAL_SECRET,
        path=f"/internal/tenant/{tenant_id}/coding-config",
        cache=redis,                        # any redis.asyncio.Redis-like client
        fresh_key=f"coding_config:{tenant_id}",
        lkg_key=f"coding_config:lkg:{tenant_id}",
        validate=_valid,
        parse=_parse,
        ttl=300,                            # fixed; or a callable body -> int for dynamic tiers
    )
    # fail-CLOSED fallback: caller decides the safe default (here: keep dual-coding on)
    cfg = await resolve_config(spec, local_fallback=static_fallback(
        Coding(profile="auto", dual_coding_required=True, source="local"),
    ))
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sm_common.internal_http import InternalClient

logger = logging.getLogger(__name__)

#: Default per-request timeout for the internal platform-api hop, in seconds.
#: The four hand-copies drifted across 2s/10s/15s/30s; this is the single default.
#: Callers may override per-spec via ``ConfigSpec.timeout``.
DEFAULT_INTERNAL_TIMEOUT_SECONDS: float = 10.0

#: Last-known-good cache TTL, in seconds (7 days). Single source of truth.
LKG_TTL_SECONDS: int = 604800

#: Fallback fresh-cache TTL used when no fixed ``ttl`` is given and/or a dynamic
#: ``ttl`` policy callable raises.
DEFAULT_FRESH_TTL_SECONDS: int = 300

#: Which tier served the resolved config (for logging/observability). Note the
#: caller's ``local_fallback`` value carries whatever source string the caller chose
#: (conventionally ``"local"``), so it is not constrained here.
ConfigSource = Literal["live", "last_known_good"]

#: Fresh-cache TTL policy: a fixed number of seconds, or a callable that derives the
#: TTL from the fetched body (e.g. per-response ``ttl_tiers``). A raising callable is
#: guarded — see ``DEFAULT_FRESH_TTL_SECONDS``.
type TtlPolicy = int | Callable[[Mapping[str, Any]], int]


class AsyncCache(Protocol):
    """Minimal async cache contract, satisfied structurally by ``redis.asyncio.Redis``.

    ``sm_common`` is a pure library and does not depend on redis; callers pass their
    own client. Only ``get`` and ``set(..., ex=...)`` are used.
    """

    async def get(self, key: str) -> Any: ...

    async def set(self, key: str, value: str, *, ex: int) -> Any: ...


@dataclass(frozen=True, slots=True)
class ConfigSpec[T]:
    """Everything ``resolve_config`` needs to resolve one config surface.

    Args:
        base_url: platform-api internal base URL (e.g. ``http://platform:8000``). If
            falsy, the HTTP tier is skipped entirely (goes straight to LKG/fallback).
        secret: the shared ``X-Internal-Secret`` value (see ``sm_common.internal_http``).
        path: internal path to GET, already tenant-scoped
            (e.g. ``/internal/tenant/{id}/journey-config``).
        cache: an ``AsyncCache`` (any ``redis.asyncio.Redis``-like client).
        fresh_key / lkg_key: Redis keys for the fresh and last-known-good entries.
        validate: predicate deciding whether a decoded body (from HTTP or cache) is a
            usable shape. An invalid body is treated as absent (miss / fetch failure).
        parse: builds the typed payload ``T`` from a validated body and the serving
            source. Async so it may do I/O (e.g. a DB read for a defensive path);
            wrap a sync mapper in ``async def`` if you have no I/O.
        ttl: fresh-cache TTL policy (fixed int or ``body -> int`` callable).
        lkg_ttl: last-known-good TTL (defaults to the shared 7-day constant).
        timeout: per-request internal-call timeout (defaults to the shared constant).
        log: optional logger; defaults to this module's logger.
    """

    base_url: str | None
    secret: str
    path: str
    cache: AsyncCache
    fresh_key: str
    lkg_key: str
    validate: Callable[[Any], bool]
    parse: Callable[[Mapping[str, Any], ConfigSource], Awaitable[T]]
    ttl: TtlPolicy = DEFAULT_FRESH_TTL_SECONDS
    lkg_ttl: int = LKG_TTL_SECONDS
    timeout: float = DEFAULT_INTERNAL_TIMEOUT_SECONDS
    log: logging.Logger | None = None


def static_fallback[T](value: T) -> Callable[[], Awaitable[T]]:
    """Wrap a precomputed value as the zero-arg async factory ``local_fallback`` expects.

    Use this when the fallback is a constant. When the fallback needs I/O (e.g. a DB
    read), pass your own ``async def`` factory instead so it runs only on total outage.
    """

    async def _factory() -> T:
        return value

    return _factory


async def resolve_config[T](
    spec: ConfigSpec[T], *, local_fallback: Callable[[], Awaitable[T]]
) -> T:
    """Resolve ``spec`` through the fresh->live->LKG->caller-fallback chain. Never raises.

    On total outage (HTTP down AND both cache tiers empty) this returns
    ``await local_fallback()`` and injects **no** default of its own — the caller owns
    the safe/fail-closed value. See the module docstring.
    """
    log = spec.log or logger

    fresh = await _read_cache(spec, spec.fresh_key, log)
    if fresh is not None:
        return await spec.parse(fresh, "live")

    body = await _fetch(spec, log)
    if body is not None:
        payload = json.dumps(body)
        await _write_cache(spec, spec.fresh_key, payload, _fresh_ttl(spec, body, log), log)
        await _write_cache(spec, spec.lkg_key, payload, spec.lkg_ttl, log)
        return await spec.parse(body, "live")

    lkg = await _read_cache(spec, spec.lkg_key, log)
    if lkg is not None:
        log.warning("config_fetch: serving last-known-good (key=%s)", spec.lkg_key)
        return await spec.parse(lkg, "last_known_good")

    log.warning("config_fetch: caller fallback (key=%s)", spec.fresh_key)
    return await local_fallback()


async def _fetch[T](spec: ConfigSpec[T], log: logging.Logger) -> Mapping[str, Any] | None:
    """GET and decode the producer body. Returns the body, or None on ANY failure
    (missing base URL, timeout, non-200, parse error, invalid shape). Never raises."""
    if not spec.base_url:
        return None
    try:
        async with InternalClient(
            spec.base_url.rstrip("/"), spec.secret, timeout=spec.timeout
        ) as client:
            resp = await client.get(spec.path)
        if resp.status_code != 200:
            return None
        body = resp.json()
    except Exception:  # network / timeout / decode — fail safe to the next tier
        log.warning("config_fetch: fetch failed (path=%s)", spec.path, exc_info=True)
        return None
    return body if spec.validate(body) else None


async def _read_cache[T](
    spec: ConfigSpec[T], key: str, log: logging.Logger
) -> Mapping[str, Any] | None:
    """Guarded cache read. A Redis error, missing key, unparseable value, or malformed
    shape is treated as a miss (returns None) — it must NEVER raise out of here."""
    try:
        raw = await spec.cache.get(key)
    except Exception:
        log.warning("config_fetch: cache read failed (key=%s)", key, exc_info=True)
        return None
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return body if spec.validate(body) else None


async def _write_cache[T](
    spec: ConfigSpec[T], key: str, payload: str, ttl: int, log: logging.Logger
) -> None:
    """Best-effort cache write — a Redis error is logged and swallowed (caching must
    never break the request)."""
    try:
        await spec.cache.set(key, payload, ex=ttl)
    except Exception:
        log.warning("config_fetch: cache write failed (key=%s)", key, exc_info=True)


def _fresh_ttl[T](spec: ConfigSpec[T], body: Mapping[str, Any], log: logging.Logger) -> int:
    """Compute the fresh-cache TTL from the policy, guarding a raising dynamic policy."""
    ttl = spec.ttl
    if not callable(ttl):
        return ttl
    try:
        return ttl(body)
    except Exception:
        # e.g. a body whose ttl_tiers is not a dict -> .values() AttributeError.
        log.warning("config_fetch: ttl policy raised; using default fresh ttl", exc_info=True)
        return DEFAULT_FRESH_TTL_SECONDS
