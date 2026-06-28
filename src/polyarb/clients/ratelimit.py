"""Per-service rate limiting and backoff.

Polymarket publishes per-service request quotas (see ``docs/API_NOTES.md``). The docs say
excess requests are *queued* rather than 429-rejected, but Cloudflare in front can still
429, so we both (a) pace ourselves with a token bucket to stay under quota and (b) back off
with jitter when a retryable error does occur.

Quotas are expressed per 10-second window in the docs; we model each as a token bucket with
``rate_per_sec = limit / 10`` and ``capacity = limit`` (a full window of burst).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class TokenBucket:
    """An asyncio-safe token bucket. ``acquire`` blocks until enough tokens have refilled."""

    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        if rate_per_sec <= 0 or capacity <= 0:
            raise ValueError("rate_per_sec and capacity must be positive")
        self._rate = rate_per_sec
        self._capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> float:
        """Current token count without refilling (for tests/introspection)."""
        return self._tokens

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens > self._capacity:
            raise ValueError(f"cannot acquire {tokens} tokens from a bucket of {self._capacity}")
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep((tokens - self._tokens) / self._rate)


@dataclass(frozen=True)
class Quota:
    """A request quota of ``limit`` requests per ``window_seconds``."""

    limit: int
    window_seconds: float = 10.0

    def bucket(self) -> TokenBucket:
        return TokenBucket(rate_per_sec=self.limit / self.window_seconds, capacity=self.limit)


# Verified 2026-06-28 from docs/API_NOTES.md. Per-endpoint where the docs distinguish; the
# "_default" entry is the service-wide general limit. Read-only endpoints only — trading
# quotas are deliberately omitted (the detector never trades).
SERVICE_QUOTAS: dict[str, dict[str, Quota]] = {
    "gamma": {
        "_default": Quota(4000),
        "/events": Quota(500),
        "/markets": Quota(300),
        "/public-search": Quota(350),
    },
    "data": {
        "_default": Quota(1000),
        "/trades": Quota(200),
        "/positions": Quota(150),
        "/closed-positions": Quota(150),
    },
    "clob": {
        "_default": Quota(9000),
        "/book": Quota(1500),
        "/price": Quota(1500),
        "/midpoint": Quota(1500),
    },
}


class ServiceLimiter:
    """Holds one token bucket per (service, endpoint), falling back to the service default."""

    def __init__(self, quotas: dict[str, dict[str, Quota]] | None = None) -> None:
        source = quotas if quotas is not None else SERVICE_QUOTAS
        self._buckets: dict[str, dict[str, TokenBucket]] = {
            service: {path: quota.bucket() for path, quota in paths.items()}
            for service, paths in source.items()
        }

    async def acquire(self, service: str, endpoint: str = "_default") -> None:
        """Wait for capacity on ``endpoint`` (the per-endpoint bucket) of ``service``."""
        try:
            buckets = self._buckets[service]
        except KeyError:
            raise KeyError(f"unknown service {service!r}") from None
        bucket = buckets.get(endpoint, buckets["_default"])
        await bucket.acquire()


async def with_backoff[T](
    fn: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[Exception], bool],
    max_attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
) -> T:
    """Call ``fn`` with exponential backoff + full jitter on retryable exceptions.

    ``is_retryable`` decides which exceptions warrant a retry (e.g. HTTP 429 / 5xx). On the
    final attempt the exception propagates unchanged.
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:
            attempt += 1
            if attempt >= max_attempts or not is_retryable(exc):
                raise
            ceiling = min(max_delay, base_delay * 2 ** (attempt - 1))
            await asyncio.sleep(random.uniform(0, ceiling))
