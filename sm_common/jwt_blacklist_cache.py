import time as _time_module
from collections import OrderedDict
from collections.abc import Awaitable, Callable


class JWTBlacklistCache:
    """In-process LRU cache for JWT blacklist negative lookups.

    Caches only negative results (JTI not blacklisted) to reduce Redis QPS.
    Positive results (JTI is blacklisted) always bypass the cache.

    TTL is the maximum staleness window: a freshly blacklisted JTI may still
    return False for up to `ttl` seconds after blacklisting, unless `invalidate`
    is called explicitly (e.g., at logout time).

    Thread safety: safe for asyncio concurrency (no await between dict mutations);
    not safe for multi-threaded use.
    """

    def __init__(
        self,
        maxsize: int = 10_000,
        ttl: float = 5.0,
        time_fn: Callable[[], float] = _time_module.monotonic,
    ) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._time_fn = time_fn
        # Maps jti -> expiry (monotonic time). OrderedDict tracks recency for LRU eviction.
        self._cache: OrderedDict[str, float] = OrderedDict()

    async def is_blacklisted(
        self,
        jti: str,
        check_fn: Callable[[str], Awaitable[bool]],
    ) -> bool:
        """Return True if the JTI is blacklisted, using cache for negative results.

        Args:
            jti: JWT ID to check.
            check_fn: Async callable that queries the authoritative store (Redis).
                      Returns True if blacklisted, False if not.
        """
        now = self._time_fn()

        if jti in self._cache:
            expiry = self._cache[jti]
            if now < expiry:
                self._cache.move_to_end(jti)
                return False
            del self._cache[jti]

        is_bl = await check_fn(jti)

        if not is_bl:
            self._cache[jti] = now + self._ttl
            self._cache.move_to_end(jti)
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

        return is_bl

    def invalidate(self, jti: str) -> None:
        """Remove a JTI from the cache (call when explicitly blacklisting a token)."""
        self._cache.pop(jti, None)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)
