"""WebSocket streaming runner: keeps an OrderBookCache fresh from live market data.

``StreamingBooks`` owns the WS connection lifecycle (reconnect + exponential
backoff with jitter), a periodic REST resync safety net (full-depth correction
every ``ws_resync_interval_s``), and an on-demand stale-token resync (draining
``cache.take_stale()`` on a shorter interval).

Design for testability
----------------------
Every external dependency is injected:

- ``ws_factory`` — callable that returns a fresh :class:`~polyarb.clients.ws.MarketWebSocket`
  for each connection attempt (inject a fake for offline tests).
- ``cache``      — the :class:`~polyarb.engine.bookcache.OrderBookCache` to keep fresh
  (inject a pre-seeded cache to test resync in isolation).
- ``sleep``      — async sleep callable (default ``asyncio.sleep``; inject
  ``asyncio.sleep(0)`` or a no-op for deterministic tests with instant backoff).

Scanner integration
-------------------
Phase 3 wires ``StreamingBooks`` into the scanner; that module is NOT touched
here.  ``streaming_enabled=False`` (the default) means this runner is never
instantiated on the existing scan path — no existing behaviour changes.

Lifecycle summary
-----------------
``run(stop)`` starts two concurrent asyncio tasks:

1. **stream loop** — open WS via ``ws_factory()``, feed messages to ``cache.apply()``,
   reconnect on any error or normal stream end using exponential backoff with jitter.
   A healthy run (≥ ``_HEALTHY_RUN_S``) resets the backoff counter.

2. **resync loop** — every ``_STALE_CHECK_INTERVAL_S`` seconds: drain
   ``cache.take_stale()`` and REST-fetch those tokens immediately.  When the
   elapsed time since the last full resync exceeds ``ws_resync_interval_s``,
   REST-fetch ALL tracked tokens as a full-depth safety net.  Fetch errors are
   logged and skipped — never fatal.

On ``stop.set()`` or ``CancelledError``, both tasks are cancelled and the
runner returns cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
import structlog

from polyarb.clients.clob import ClobClient
from polyarb.clients.ws import MarketWebSocket
from polyarb.config import Settings
from polyarb.engine.bookcache import RESYNC_BATCH_SIZE, OrderBookCache
from polyarb.models import OrderBook

log = structlog.get_logger("polyarb.streaming")

# Initial reconnect backoff in seconds; doubles (with jitter) up to ws_max_backoff_s.
_BASE_BACKOFF_S: float = 1.0

# A WS run longer than this is considered healthy; resets the backoff counter.
_HEALTHY_RUN_S: float = 30.0

# How often the resync loop checks for stale tokens.  Intentionally shorter
# than ws_resync_interval_s so hash-revert detections trigger a REST fetch
# quickly.
_STALE_CHECK_INTERVAL_S: float = 5.0

# Maximum concurrent REST fetches in a single resync pass.
_RESYNC_CONCURRENCY: int = RESYNC_BATCH_SIZE


class StreamingBooks:
    """Keeps an :class:`~polyarb.engine.bookcache.OrderBookCache` fresh via
    a live WS stream + periodic REST resync.

    Parameters
    ----------
    token_ids:
        The CLOB token IDs to subscribe to and maintain.
    clob:
        A :class:`~polyarb.clients.clob.ClobClient` for REST resync fetches.
        Must be externally managed (opened/closed by the caller).
    settings:
        Runtime configuration (``ws_resync_interval_s``, ``ws_max_backoff_s``).
    ws_factory:
        Zero-argument callable that returns a fresh
        :class:`~polyarb.clients.ws.MarketWebSocket` for each connection
        attempt.  Defaults to ``lambda: MarketWebSocket()``.  Inject a fake
        for offline tests.
    cache:
        The cache to populate.  Defaults to a new empty
        :class:`~polyarb.engine.bookcache.OrderBookCache`.  Inject a
        pre-seeded cache for tests.
    sleep:
        Async sleep callable (signature: ``async (seconds: float) -> None``).
        Defaults to ``asyncio.sleep``.  Inject ``asyncio.sleep(0)`` or a
        synchronous no-op (wrapped in ``asyncio.coroutine``) for tests that
        need instant backoff.
    """

    def __init__(
        self,
        token_ids: Sequence[str],
        *,
        clob: ClobClient,
        settings: Settings,
        ws_factory: Callable[[], MarketWebSocket] | None = None,
        cache: OrderBookCache | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._token_ids: list[str] = list(token_ids)
        self._clob = clob
        self._settings = settings
        self._ws_factory: Callable[[], MarketWebSocket] = ws_factory or (lambda: MarketWebSocket())
        self._cache: OrderBookCache = cache if cache is not None else OrderBookCache()
        self._sleep = sleep

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def books(self) -> dict[str, OrderBook]:
        """Return a snapshot of all currently-cached order books."""
        return self._cache.books()

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Drive the runner until ``stop`` is set or the task is cancelled.

        Runs two concurrent asyncio tasks (stream loop + resync loop).  Both
        tasks shut down when ``stop`` is set.  On ``CancelledError`` the stop
        event is signalled so the resync task also exits cleanly.
        """
        if stop is None:
            stop = asyncio.Event()

        resync_task = asyncio.create_task(self._resync_loop(stop))
        try:
            await self._stream_loop(stop)
        except asyncio.CancelledError:
            stop.set()  # ensure resync task also exits
            raise
        finally:
            resync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await resync_task
            log.info("streaming_stopped", tracked_tokens=len(self._token_ids))

    # ------------------------------------------------------------------
    # Internal: WS stream loop
    # ------------------------------------------------------------------

    async def _stream_loop(self, stop: asyncio.Event) -> None:
        """Open WS, feed messages to cache; reconnect with exponential backoff.

        Backoff starts at ``_BASE_BACKOFF_S``, doubles on each disconnect (with
        25 % jitter), and is capped at ``settings.ws_max_backoff_s``.  A
        healthy run (stream open for ≥ ``_HEALTHY_RUN_S`` seconds) resets the
        backoff to the base value.
        """
        backoff = _BASE_BACKOFF_S

        while not stop.is_set():
            ws = self._ws_factory()
            loop = asyncio.get_running_loop()
            t_start = loop.time()

            try:
                async for msg in ws.stream(self._token_ids):
                    if stop.is_set():
                        return
                    self._cache.apply(msg)
                # Stream ended without error (server closed cleanly).
                log.info("ws_stream_ended")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("ws_disconnect", error=repr(exc))

            if stop.is_set():
                return

            elapsed = loop.time() - t_start
            if elapsed >= _HEALTHY_RUN_S:
                backoff = _BASE_BACKOFF_S  # healthy run → reset
                log.debug("ws_backoff_reset")

            jitter = random.uniform(0.0, backoff * 0.25)
            wait_s = min(backoff + jitter, self._settings.ws_max_backoff_s)
            log.info("ws_reconnecting", delay_s=round(wait_s, 2))

            await self._sleep(wait_s)

            backoff = min(backoff * 2.0, self._settings.ws_max_backoff_s)

    # ------------------------------------------------------------------
    # Internal: resync loop
    # ------------------------------------------------------------------

    async def _resync_loop(self, stop: asyncio.Event) -> None:
        """Drain stale tokens on demand + periodic full resync.

        Wakes every ``_STALE_CHECK_INTERVAL_S`` seconds.  On each wake:
        1. Drain ``cache.take_stale()`` and REST-fetch those tokens immediately
           (on-demand, driven by A3 hash-revert or other staleness signals).
        2. If ``ws_resync_interval_s`` has elapsed since the last full resync,
           REST-fetch ALL tracked tokens as a full-depth safety net.

        Individual fetch errors (``httpx.HTTPError``) are logged and skipped;
        they never abort the loop.
        """
        last_full_resync = 0.0
        loop = asyncio.get_running_loop()

        while not stop.is_set():
            await self._sleep(_STALE_CHECK_INTERVAL_S)
            if stop.is_set():
                return

            # On-demand: flush stale tokens first (they have a known integrity
            # problem and benefit from being resynced quickly).
            stale = self._cache.take_stale()
            if stale:
                log.info("resync_stale_tokens", count=len(stale))
                await self._resync_tokens(sorted(stale))

            # Periodic full resync: correct any drift the WS stream may have
            # introduced (deep levels, hash reverts not yet seen, etc.).
            now = loop.time()
            if now - last_full_resync >= self._settings.ws_resync_interval_s:
                log.info("resync_full", token_count=len(self._token_ids))
                await self._resync_tokens(self._token_ids)
                last_full_resync = now

    async def _resync_tokens(self, tokens: Sequence[str]) -> None:
        """REST-fetch books for ``tokens`` and seed the cache.

        Fetches are throttled to ``_RESYNC_CONCURRENCY`` concurrent requests
        (the ClobClient has its own rate limiter; the semaphore prevents a
        thundering-herd burst against the exchange).  Errors are logged and
        skipped — a failed fetch leaves the cached book unchanged.
        """
        if not tokens:
            return

        sem = asyncio.Semaphore(_RESYNC_CONCURRENCY)

        async def _one(token_id: str) -> None:
            async with sem:
                try:
                    book = await self._clob.get_order_book(token_id)
                    self._cache.seed(book)
                except httpx.HTTPError as exc:
                    log.warning(
                        "resync_fetch_error",
                        token_id=token_id,
                        error=repr(exc),
                    )
                except Exception as exc:
                    log.warning(
                        "resync_unexpected_error",
                        token_id=token_id,
                        error=repr(exc),
                    )

        await asyncio.gather(*(_one(t) for t in tokens))
