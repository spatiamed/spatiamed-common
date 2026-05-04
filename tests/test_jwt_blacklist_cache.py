import pytest

from sm_common.jwt_blacklist_cache import JWTBlacklistCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeClock:
    """Monotonic clock whose value we control in tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def blacklisted(*jtis: str):
    """Returns a check_fn that reports given JTIs as blacklisted, rest as not."""
    bl_set = set(jtis)

    async def check(jti: str) -> bool:
        return jti in bl_set

    return check


# ---------------------------------------------------------------------------
# Negative-result caching
# ---------------------------------------------------------------------------

class TestNegativeCaching:
    @pytest.mark.asyncio
    async def test_negative_result_cached(self):
        clock = FakeClock()
        cache = JWTBlacklistCache(ttl=10.0, time_fn=clock)
        calls = []

        async def check(jti: str) -> bool:
            calls.append(jti)
            return False

        for _ in range(5):
            result = await cache.is_blacklisted("jti-a", check)
            assert result is False

        assert len(calls) == 1, "check_fn should be called only once while cache is warm"

    @pytest.mark.asyncio
    async def test_positive_result_not_cached(self):
        clock = FakeClock()
        cache = JWTBlacklistCache(ttl=10.0, time_fn=clock)
        calls = []

        async def check(jti: str) -> bool:
            calls.append(jti)
            return True

        for _ in range(5):
            result = await cache.is_blacklisted("jti-bl", check)
            assert result is True

        assert len(calls) == 5, "blacklisted JTI must never be cached"

    @pytest.mark.asyncio
    async def test_call_reduction_at_steady_state(self):
        """100 lookups across 5 unique JTIs → check_fn called exactly 5 times."""
        clock = FakeClock()
        cache = JWTBlacklistCache(ttl=60.0, time_fn=clock)
        calls = []

        async def check(jti: str) -> bool:
            calls.append(jti)
            return False

        jtis = [f"jti-{i}" for i in range(5)]
        for i in range(100):
            await cache.is_blacklisted(jtis[i % 5], check)

        assert len(calls) == 5
        assert cache.size == 5


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    @pytest.mark.asyncio
    async def test_entry_expired_after_ttl(self):
        clock = FakeClock()
        cache = JWTBlacklistCache(ttl=5.0, time_fn=clock)
        call_count = []

        async def check(jti: str) -> bool:
            call_count.append(jti)
            return False

        await cache.is_blacklisted("jti-x", check)
        assert len(call_count) == 1

        clock.advance(6.0)  # past TTL
        await cache.is_blacklisted("jti-x", check)
        assert len(call_count) == 2

    @pytest.mark.asyncio
    async def test_stale_window_design_tradeoff(self):
        """Blacklisted JTI without invalidate() returns False during TTL window (by design)."""
        clock = FakeClock()
        cache = JWTBlacklistCache(ttl=5.0, time_fn=clock)
        blacklisted_flag = [False]

        async def check(jti: str) -> bool:
            return blacklisted_flag[0]

        # Warm cache with negative result
        result = await cache.is_blacklisted("jti-y", check)
        assert result is False

        # Now blacklist the JTI externally — but DON'T call invalidate()
        blacklisted_flag[0] = True

        # During TTL window: cache still returns False (stale — acceptable by design)
        result = await cache.is_blacklisted("jti-y", check)
        assert result is False, "stale negative cached within TTL (expected design tradeoff)"

        # After TTL: cache expires, next check hits Redis and returns True
        clock.advance(6.0)
        result = await cache.is_blacklisted("jti-y", check)
        assert result is True, "after TTL, blacklisted JTI must be denied"

    @pytest.mark.asyncio
    async def test_invalidate_removes_stale_entry_immediately(self):
        """Calling invalidate() bypasses the TTL stale window."""
        clock = FakeClock()
        cache = JWTBlacklistCache(ttl=10.0, time_fn=clock)
        blacklisted_flag = [False]

        async def check(jti: str) -> bool:
            return blacklisted_flag[0]

        await cache.is_blacklisted("jti-z", check)
        blacklisted_flag[0] = True

        cache.invalidate("jti-z")

        result = await cache.is_blacklisted("jti-z", check)
        assert result is True


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------

class TestLRUEviction:
    @pytest.mark.asyncio
    async def test_oldest_evicted_when_full(self):
        clock = FakeClock()
        cache = JWTBlacklistCache(maxsize=3, ttl=60.0, time_fn=clock)

        async def not_bl(jti: str) -> bool:
            return False

        # Insert 3 items: a, b, c
        for jti in ["a", "b", "c"]:
            await cache.is_blacklisted(jti, not_bl)
        assert cache.size == 3

        # Insert a 4th — oldest "a" should be evicted
        await cache.is_blacklisted("d", not_bl)
        assert cache.size == 3
        assert "a" not in cache._cache
        assert "d" in cache._cache

    @pytest.mark.asyncio
    async def test_recently_used_item_not_evicted(self):
        """Touch item 'a' mid-stream → 'b' (oldest untouched) should be evicted."""
        clock = FakeClock()
        cache = JWTBlacklistCache(maxsize=3, ttl=60.0, time_fn=clock)
        calls = []

        async def not_bl(jti: str) -> bool:
            calls.append(jti)
            return False

        # Insert a, b, c
        for jti in ["a", "b", "c"]:
            await cache.is_blacklisted(jti, not_bl)

        # Touch "a" — moves it to most-recently-used end
        await cache.is_blacklisted("a", not_bl)

        # Insert "d" — should evict "b" (oldest untouched), NOT "a"
        await cache.is_blacklisted("d", not_bl)
        assert cache.size == 3
        assert "b" not in cache._cache
        assert "a" in cache._cache
        assert "d" in cache._cache


# ---------------------------------------------------------------------------
# invalidate / clear
# ---------------------------------------------------------------------------

class TestManagement:
    @pytest.mark.asyncio
    async def test_invalidate_nonexistent_is_noop(self):
        cache = JWTBlacklistCache()
        cache.invalidate("does-not-exist")  # must not raise

    @pytest.mark.asyncio
    async def test_clear_empties_cache(self):
        clock = FakeClock()
        cache = JWTBlacklistCache(ttl=60.0, time_fn=clock)
        calls = []

        async def not_bl(jti: str) -> bool:
            calls.append(jti)
            return False

        await cache.is_blacklisted("j1", not_bl)
        await cache.is_blacklisted("j2", not_bl)
        assert cache.size == 2

        cache.clear()
        assert cache.size == 0

        # After clear, next lookup must hit check_fn again
        await cache.is_blacklisted("j1", not_bl)
        assert len(calls) == 3  # j1, j2 (initial), then j1 again after clear

    @pytest.mark.asyncio
    async def test_size_reflects_cached_negatives_only(self):
        clock = FakeClock()
        cache = JWTBlacklistCache(ttl=60.0, time_fn=clock)

        async def check(jti: str) -> bool:
            return jti == "bl"

        await cache.is_blacklisted("ok", check)   # negative → cached
        await cache.is_blacklisted("bl", check)   # positive → not cached
        assert cache.size == 1
