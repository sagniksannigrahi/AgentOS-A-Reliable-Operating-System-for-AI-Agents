"""Redis coordination helpers.

Redis is deliberately *not* on the correctness path: PostgreSQL holds all durable state. If Redis
is unavailable, the API skips rate limiting (fail-open, logged) and workers fall back to polling.
"""

from __future__ import annotations

import time

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.observability.logging import get_logger

log = get_logger(__name__)

WAKEUP_KEY = "agentos:runs:wakeup"


class RedisCoordinator:
    def __init__(self, client: aioredis.Redis | None) -> None:
        self.client = client
        self._last_error_log = 0.0

    @classmethod
    def from_url(cls, url: str | None) -> RedisCoordinator:
        if not url:
            return cls(None)
        client = aioredis.from_url(url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
        return cls(client)

    def _warn(self, op: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_error_log > 30:
            log.warning("redis unavailable; degrading", op=op, error=str(exc)[:200])
            self._last_error_log = now

    async def ping(self) -> bool:
        if self.client is None:
            return False
        try:
            return bool(await self.client.ping())
        except (RedisError, OSError) as exc:
            self._warn("ping", exc)
            return False

    async def allow(self, key: str, limit: int, window_s: int = 60) -> bool:
        """Fixed-window rate limit. Fails open if Redis is down."""
        if self.client is None or limit <= 0:
            return True
        bucket = f"agentos:rl:{key}:{int(time.time() // window_s)}"
        try:
            async with self.client.pipeline(transaction=True) as pipe:
                pipe.incr(bucket)
                pipe.expire(bucket, window_s + 5)
                count, _ = await pipe.execute()
            return int(count) <= limit
        except (RedisError, OSError) as exc:
            self._warn("rate_limit", exc)
            return True

    async def notify_work(self) -> None:
        if self.client is None:
            return
        try:
            async with self.client.pipeline(transaction=True) as pipe:
                pipe.lpush(WAKEUP_KEY, "1")
                pipe.ltrim(WAKEUP_KEY, 0, 99)
                await pipe.execute()
        except (RedisError, OSError) as exc:
            self._warn("notify", exc)

    async def wait_for_work(self, timeout_s: float) -> bool:
        """Block up to ``timeout_s`` for a wake-up signal. Returns False on timeout/unavailable."""
        if self.client is None:
            return False
        try:
            res = await self.client.blpop([WAKEUP_KEY], timeout=max(1, int(timeout_s)))
            return res is not None
        except (RedisError, OSError) as exc:
            self._warn("wait", exc)
            return False

    async def cache_get(self, key: str) -> str | None:
        if self.client is None:
            return None
        try:
            return await self.client.get(f"agentos:cache:{key}")
        except (RedisError, OSError) as exc:
            self._warn("cache_get", exc)
            return None

    async def cache_set(self, key: str, value: str, ttl_s: int) -> None:
        if self.client is None:
            return
        try:
            await self.client.set(f"agentos:cache:{key}", value, ex=ttl_s)
        except (RedisError, OSError) as exc:
            self._warn("cache_set", exc)

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.aclose()
            except (RedisError, OSError):
                pass
