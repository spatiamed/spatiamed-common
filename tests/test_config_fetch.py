"""Tests for sm_common.config_fetch — the unified platform-api + Redis + LKG resolver.

Covers the full tier chain plus each hardening guarantee (guarded cache read, guarded
dynamic-TTL policy, single timeout, and — critically — the caller-controlled fail-safe
fallback with NO library default).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
import respx
from httpx import Response

from sm_common import config_fetch
from sm_common.config_fetch import (
    DEFAULT_FRESH_TTL_SECONDS,
    LKG_TTL_SECONDS,
    ConfigSpec,
    resolve_config,
    static_fallback,
)

BASE_URL = "http://platform.internal"
SECRET = "s3cr3t-internal"
PATH = "/internal/tenant/t1/journey-config"
FRESH_KEY = "journey_config:t1"
LKG_KEY = "journey_config:lkg:t1"


# --------------------------------------------------------------------------- fakes


class FakeRedis:
    """In-memory async cache satisfying config_fetch.AsyncCache."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets: list[tuple[str, str, int]] = []
        self.raise_on_get = False
        self.raise_on_set = False

    async def get(self, key: str) -> Any:
        if self.raise_on_get:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> Any:
        if self.raise_on_set:
            raise RuntimeError("redis down")
        self.sets.append((key, value, ex))
        self.store[key] = value


@dataclass(frozen=True)
class DummyConfig:
    marker: str
    source: str


def _valid(body: Any) -> bool:
    return isinstance(body, dict) and isinstance(body.get("resolved"), dict)


async def _parse(body: Mapping[str, Any], source: str) -> DummyConfig:
    return DummyConfig(marker=body["resolved"]["marker"], source=source)


FALLBACK = DummyConfig(marker="CALLER_FALLBACK", source="local")


def _spec(cache: FakeRedis, **overrides: Any) -> ConfigSpec[DummyConfig]:
    kwargs: dict[str, Any] = dict(
        base_url=BASE_URL,
        secret=SECRET,
        path=PATH,
        cache=cache,
        fresh_key=FRESH_KEY,
        lkg_key=LKG_KEY,
        validate=_valid,
        parse=_parse,
    )
    kwargs.update(overrides)
    return ConfigSpec(**kwargs)


def _body(marker: str) -> dict[str, Any]:
    return {"resolved": {"marker": marker}}


# --------------------------------------------------------------------------- tests


@respx.mock
@pytest.mark.asyncio
async def test_cache_hit_serves_live_without_http() -> None:
    cache = FakeRedis()
    cache.store[FRESH_KEY] = json.dumps(_body("from_cache"))
    route = respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(200, json=_body("http")))

    result = await resolve_config(_spec(cache), local_fallback=static_fallback(FALLBACK))

    assert result == DummyConfig(marker="from_cache", source="live")
    assert route.call_count == 0  # fresh cache short-circuits the HTTP hop
    assert cache.sets == []  # nothing re-written on a fresh hit


@respx.mock
@pytest.mark.asyncio
async def test_cache_miss_fetches_http_and_writes_fresh_plus_lkg() -> None:
    cache = FakeRedis()
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(200, json=_body("from_http")))

    result = await resolve_config(_spec(cache, ttl=120), local_fallback=static_fallback(FALLBACK))

    assert result == DummyConfig(marker="from_http", source="live")
    # both fresh (ttl=120) and LKG (7-day constant) written
    written = {key: ex for key, _val, ex in cache.sets}
    assert written[FRESH_KEY] == 120
    assert written[LKG_KEY] == LKG_TTL_SECONDS
    assert json.loads(cache.store[FRESH_KEY]) == _body("from_http")


@respx.mock
@pytest.mark.asyncio
async def test_http_failure_serves_last_known_good() -> None:
    cache = FakeRedis()
    cache.store[LKG_KEY] = json.dumps(_body("stale_but_good"))
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(503))

    result = await resolve_config(_spec(cache), local_fallback=static_fallback(FALLBACK))

    assert result == DummyConfig(marker="stale_but_good", source="last_known_good")


@respx.mock
@pytest.mark.asyncio
async def test_total_outage_returns_caller_fallback_not_library_default() -> None:
    """Both HTTP and cache fail: the CALLER's fallback is returned verbatim, and the
    library injects no domain default of its own (the compliance-critical property)."""
    cache = FakeRedis()  # empty: no fresh, no LKG
    respx.get(f"{BASE_URL}{PATH}").mock(side_effect=Exception("connect error"))

    fail_closed = DummyConfig(marker="DUAL_CODING_STILL_REQUIRED", source="local")
    result = await resolve_config(_spec(cache), local_fallback=static_fallback(fail_closed))

    assert result is fail_closed  # exact object the caller passed — nothing substituted


@respx.mock
@pytest.mark.asyncio
async def test_malformed_cache_is_guarded_and_falls_through() -> None:
    """A poisoned/unparseable fresh cache entry must be a miss, never an exception —
    fixes the notification_service fail-unsafe json.loads."""
    cache = FakeRedis()
    cache.store[FRESH_KEY] = "}{ not json at all"
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(200, json=_body("recovered")))

    result = await resolve_config(_spec(cache), local_fallback=static_fallback(FALLBACK))

    assert result == DummyConfig(marker="recovered", source="live")


@respx.mock
@pytest.mark.asyncio
async def test_redis_get_error_is_guarded() -> None:
    cache = FakeRedis()
    cache.raise_on_get = True  # every read raises
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(200, json=_body("http")))

    # fresh read raises -> guarded miss -> HTTP; LKG read also raises -> guarded.
    result = await resolve_config(_spec(cache), local_fallback=static_fallback(FALLBACK))
    assert result == DummyConfig(marker="http", source="live")


@respx.mock
@pytest.mark.asyncio
async def test_dynamic_ttl_tiers_non_dict_is_guarded() -> None:
    """Reproduces the QueueCare journey copy's bug: a ttl policy that reads
    body['ttl_tiers'].values() AttributeErrors when ttl_tiers is not a dict. The
    resolver must guard it and fall back to DEFAULT_FRESH_TTL_SECONDS."""
    tier_seconds = {"sensitive": 60, "standard": 300}

    def buggy_ttl(body: Mapping[str, Any]) -> int:
        tiers = body.get("ttl_tiers", {})
        # Unguarded on purpose — if tiers is a list this raises AttributeError.
        return min((tier_seconds.get(t, 300) for t in tiers.values()), default=300)

    cache = FakeRedis()
    poisoned = {"resolved": {"marker": "http"}, "ttl_tiers": ["sensitive"]}  # a LIST
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(200, json=poisoned))

    # Must not raise; must still cache + return live.
    result = await resolve_config(
        _spec(cache, ttl=buggy_ttl), local_fallback=static_fallback(FALLBACK)
    )

    assert result == DummyConfig(marker="http", source="live")
    fresh_ttl = next(ex for key, _v, ex in cache.sets if key == FRESH_KEY)
    assert fresh_ttl == DEFAULT_FRESH_TTL_SECONDS


@respx.mock
@pytest.mark.asyncio
async def test_dynamic_ttl_tiers_valid_dict_is_used() -> None:
    tier_seconds = {"sensitive": 60, "standard": 300}

    def ttl(body: Mapping[str, Any]) -> int:
        tiers = body["ttl_tiers"]
        return min((tier_seconds[t] for t in tiers.values()), default=300)

    cache = FakeRedis()
    body = {"resolved": {"marker": "http"}, "ttl_tiers": {"a": "sensitive", "b": "standard"}}
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(200, json=body))

    await resolve_config(_spec(cache, ttl=ttl), local_fallback=static_fallback(FALLBACK))

    fresh_ttl = next(ex for key, _v, ex in cache.sets if key == FRESH_KEY)
    assert fresh_ttl == 60


@pytest.mark.asyncio
async def test_timeout_override_is_passed_to_internal_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, base_url: str, secret: str, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, path: str) -> Response:
            return Response(200, json=_body("http"))

    monkeypatch.setattr(config_fetch, "InternalClient", FakeClient)
    cache = FakeRedis()

    result = await resolve_config(
        _spec(cache, timeout=2.5), local_fallback=static_fallback(FALLBACK)
    )

    assert result == DummyConfig(marker="http", source="live")
    assert captured["timeout"] == 2.5


@respx.mock
@pytest.mark.asyncio
async def test_missing_base_url_skips_http_and_uses_lkg() -> None:
    cache = FakeRedis()
    cache.store[LKG_KEY] = json.dumps(_body("lkg"))

    result = await resolve_config(
        _spec(cache, base_url=None), local_fallback=static_fallback(FALLBACK)
    )
    assert result == DummyConfig(marker="lkg", source="last_known_good")


@respx.mock
@pytest.mark.asyncio
async def test_invalid_http_body_shape_is_rejected() -> None:
    cache = FakeRedis()
    cache.store[LKG_KEY] = json.dumps(_body("lkg"))
    # 200 but wrong shape (validate() fails) -> treated as fetch failure -> LKG.
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(200, json={"wrong": "shape"}))

    result = await resolve_config(_spec(cache), local_fallback=static_fallback(FALLBACK))
    assert result == DummyConfig(marker="lkg", source="last_known_good")


@respx.mock
@pytest.mark.asyncio
async def test_cache_write_failure_does_not_break_request() -> None:
    cache = FakeRedis()
    cache.raise_on_set = True  # writes blow up
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=Response(200, json=_body("http")))

    result = await resolve_config(_spec(cache), local_fallback=static_fallback(FALLBACK))
    assert result == DummyConfig(marker="http", source="live")
