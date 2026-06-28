"""Tests for the per-service rate limiter and backoff (offline, no network)."""

from __future__ import annotations

import asyncio
import time

import pytest

from polyarb.clients.ratelimit import (
    Quota,
    ServiceLimiter,
    TokenBucket,
    with_backoff,
)


def test_full_bucket_acquires_immediately() -> None:
    async def run() -> float:
        bucket = TokenBucket(rate_per_sec=10, capacity=5)
        start = time.monotonic()
        for _ in range(5):
            await bucket.acquire()
        return time.monotonic() - start

    assert asyncio.run(run()) < 0.05


def test_draining_bucket_blocks_until_refill() -> None:
    async def run() -> float:
        bucket = TokenBucket(rate_per_sec=20, capacity=2)  # 1 token per 50ms
        await bucket.acquire()
        await bucket.acquire()
        start = time.monotonic()
        await bucket.acquire()  # must wait ~50ms for one token to refill
        return time.monotonic() - start

    waited = asyncio.run(run())
    assert 0.03 < waited < 0.2


def test_acquire_more_than_capacity_raises() -> None:
    bucket = TokenBucket(rate_per_sec=10, capacity=5)
    with pytest.raises(ValueError):
        asyncio.run(bucket.acquire(6))


def test_quota_bucket_rate() -> None:
    bucket = Quota(limit=500, window_seconds=10).bucket()
    assert bucket.tokens == 500


def test_service_limiter_unknown_service() -> None:
    limiter = ServiceLimiter()
    with pytest.raises(KeyError):
        asyncio.run(limiter.acquire("nope"))


def test_service_limiter_endpoint_fallback() -> None:
    # An unknown endpoint falls back to the service default bucket without error.
    limiter = ServiceLimiter()
    asyncio.run(limiter.acquire("gamma", "/something-unmapped"))
    asyncio.run(limiter.acquire("clob", "/book"))


def test_backoff_retries_then_succeeds() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("429")
        return "ok"

    result = asyncio.run(
        with_backoff(flaky, is_retryable=lambda e: isinstance(e, ConnectionError), base_delay=0)
    )
    assert result == "ok"
    assert calls["n"] == 3


def test_backoff_stops_on_non_retryable() -> None:
    async def boom() -> str:
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        asyncio.run(
            with_backoff(boom, is_retryable=lambda e: isinstance(e, ConnectionError), base_delay=0)
        )


def test_backoff_exhausts_attempts() -> None:
    calls = {"n": 0}

    async def always_fail() -> str:
        calls["n"] += 1
        raise ConnectionError("429")

    with pytest.raises(ConnectionError):
        asyncio.run(
            with_backoff(
                always_fail,
                is_retryable=lambda e: True,
                max_attempts=3,
                base_delay=0,
            )
        )
    assert calls["n"] == 3
