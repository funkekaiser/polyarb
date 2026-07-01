"""WebSocket streaming runner: keeps an OrderBookCache fresh from live market data.

This is the **primary** book-read path (WebSocket-first; the REST resync is the backup/
correction net). ``StreamingBooks`` owns:

- the WS connection lifecycle — reconnect + exponential backoff with jitter;
- a **stall watchdog (R5)** — a connection that is open (ping-alive) but silent for
  ``ws_stall_timeout_s`` is force-dropped and reconnected, with a metric, so a dead-but-connected
  feed cannot quietly degrade the scanner to a 60 s REST poll;
- **dynamic (un)subscription (R6)** — ``set_tokens`` adds/drops tokens on the *live* connection
  (no reconnect) and evicts dropped tokens from the cache;
- a periodic full-depth REST resync + on-demand stale-token resync (the backup read);
- **streaming observability (R8)** — Prometheus metrics + a WS heartbeat file pulsed on every
  applied message or successful resync, so ``polyarb healthcheck`` fails when the cache is frozen;
- **per-token freshness tracking (R2)** — ``fresh_books`` exposes only books refreshed within a
  wall-clock window, distinct from a book's own last-change timestamp.

Design for testability
----------------------
Every external dependency is injected: ``ws_factory`` (a ``MarketWebSocket`` factory), ``cache``
(the :class:`~polyarb.engine.bookcache.OrderBookCache`), and ``sleep`` (async sleep callable —
inject ``asyncio.sleep(0)`` for instant backoff). No real network or wall-clock dependence in the
control flow; freshness uses the event loop's monotonic clock.

Lifecycle
---------
``run(stop)`` starts two concurrent tasks — the **stream loop** (consume WS → cache) and the
**resync loop** (REST safety net + heartbeat/metrics). Both stop when ``stop`` is set or the run
task is cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

import httpx
import structlog

from polyarb.clients.clob import ClobClient
from polyarb.clients.ws import MarketWebSocket
from polyarb.config import Settings
from polyarb.engine import heartbeat, metrics
from polyarb.engine.bookcache import RESYNC_BATCH_SIZE, OrderBookCache
from polyarb.models import OrderBook

log = structlog.get_logger("polyarb.streaming")

# Initial reconnect backoff in seconds; doubles (with jitter) up to ws_max_backoff_s.
_BASE_BACKOFF_S: float = 1.0

# A WS run longer than this is considered healthy; resets the backoff counter.
_HEALTHY_RUN_S: float = 30.0

# How often the resync loop checks for stale tokens.  Intentionally shorter
# than ws_resync_interval_s so hash-revert detections trigger a REST fetch
# quickly.  Also the cadence at which metrics + the WS heartbeat are refreshed.
_STALE_CHECK_INTERVAL_S: float = 5.0

# Maximum concurrent REST fetches in a single resync pass.
_RESYNC_CONCURRENCY: int = RESYNC_BATCH_SIZE


class StreamingBooks:
    """Keeps an :class:`~polyarb.engine.bookcache.OrderBookCache` fresh via a live WS stream +
    periodic REST resync, with a stall watchdog, dynamic subscription, and liveness metrics.

    Parameters
    ----------
    token_ids:
        The CLOB token IDs to subscribe to and maintain.
    clob:
        A :class:`~polyarb.clients.clob.ClobClient` for REST resync fetches (externally managed).
    settings:
        Runtime configuration (``ws_resync_interval_s``, ``ws_max_backoff_s``,
        ``ws_stall_timeout_s``, ``ws_heartbeat_path``).
    ws_factory:
        Zero-arg callable returning a fresh :class:`~polyarb.clients.ws.MarketWebSocket` per
        connection attempt.  Defaults to ``lambda: MarketWebSocket()``.  Inject a fake for tests.
    cache:
        The cache to populate.  Defaults to a new empty cache.  Inject a pre-seeded cache for tests.
    sleep:
        Async sleep callable.  Defaults to ``asyncio.sleep``.  Inject ``asyncio.sleep(0)`` for
        instant backoff in tests.
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
        self._token_ids: list[str] = sorted(set(token_ids))
        # O(1) membership mirror of _token_ids (kept in sync in set_tokens); used by the resync
        # eviction re-check so an in-flight fetch can't resurrect a just-dropped token.
        self._tracked: set[str] = set(self._token_ids)
        self._clob = clob
        self._settings = settings
        self._ws_factory: Callable[[], MarketWebSocket] = ws_factory or (lambda: MarketWebSocket())
        self._cache: OrderBookCache = cache if cache is not None else OrderBookCache()
        self._sleep = sleep
        # R6 — pending subscribe/unsubscribe ops to apply to the LIVE connection (drained by the
        # stream loop). A reconnect re-subscribes the full current set, so the queue is cleared on
        # each fresh connect and only matters between reconnects.
        self._control: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # R2 — per-token monotonic time of the last applied delta or resync (loop.time()).
        self._last_update: dict[str, float] = {}
        # R8 — wall-clock epoch of the last applied message / last successful resync (heartbeat).
        self._last_message_wall: float = 0.0
        self._last_resync_wall: float = 0.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def books(self) -> dict[str, OrderBook]:
        """Return a snapshot of all currently-cached order books (no freshness filter)."""
        return self._cache.books()

    def fresh_books(self, max_age_s: float | None = None) -> dict[str, OrderBook]:
        """Books refreshed (delta or resync) within ``max_age_s`` wall-seconds (R2).

        Uses the event loop's monotonic clock (same clock the runner stamps with), so it is
        immune to system-clock changes. ``max_age_s <= 0`` (or None → ``ws_freshness_s``) disables
        the guard and returns every cached book. A token with no recorded update time is treated as
        stale and dropped — only ever a false-negative (a missed opp), never a fabricated one.
        """
        if max_age_s is None:
            max_age_s = self._settings.ws_freshness_s
        all_books = self._cache.books()
        if max_age_s <= 0:
            return all_books
        now = asyncio.get_running_loop().time()
        return {
            tok: book
            for tok, book in all_books.items()
            if now - self._last_update.get(tok, float("-inf")) <= max_age_s
        }

    def scoped_fresh_books(
        self, needed: Iterable[str], max_age_s: float | None = None
    ) -> tuple[dict[str, OrderBook], int]:
        """Fresh books scoped to ``needed`` plus the count of needed tokens dropped for staleness.

        One materialisation pass (vs a separate ``books()`` + ``fresh_books()``): the scanner's
        streaming path uses this both to read and to report ``stale_dropped``. A token present in
        the cache but not refreshed within ``max_age_s`` counts as stale-dropped; one absent from
        the cache entirely is simply not-yet-known (not counted).
        """
        if max_age_s is None:
            max_age_s = self._settings.ws_freshness_s
        all_books = self._cache.books()
        now = asyncio.get_running_loop().time()
        fresh: dict[str, OrderBook] = {}
        stale_dropped = 0
        for tok in needed:
            book = all_books.get(tok)
            if book is None:
                continue
            if max_age_s <= 0 or now - self._last_update.get(tok, float("-inf")) <= max_age_s:
                fresh[tok] = book
            else:
                stale_dropped += 1
        return fresh, stale_dropped

    def set_tokens(self, token_ids: Iterable[str]) -> None:
        """Update the tracked token set (R6) — dynamic subscribe/unsubscribe without a reconnect.

        Diffs against the current set: newly-discovered tokens are ``subscribe``\\d and dropped
        tokens are ``unsubscribe``\\d on the live connection (ops queued for the stream loop), and
        dropped tokens are evicted from the cache + freshness map to keep both bounded. The new set
        also feeds the next periodic REST resync and the next (re)connect's initial subscription.
        """
        new = sorted(set(token_ids))
        if new == self._token_ids:
            return
        added = sorted(set(new) - set(self._token_ids))
        dropped = sorted(set(self._token_ids) - set(new))
        self._token_ids = new
        self._tracked = set(new)
        if added:
            self._control.put_nowait({"operation": "subscribe", "assets_ids": added})
        if dropped:
            self._control.put_nowait({"operation": "unsubscribe", "assets_ids": dropped})
        for token_id in dropped:
            self._cache.evict(token_id)
            self._last_update.pop(token_id, None)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Drive the runner until ``stop`` is set or the task is cancelled.

        Runs the stream loop + resync loop concurrently; both shut down when ``stop`` is set. On
        ``CancelledError`` the stop event is signalled so the resync task also exits cleanly.
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
        """Open WS, feed messages to the cache; reconnect with exponential backoff.

        Each inbound message is awaited with a ``ws_stall_timeout_s`` deadline (R5): if the open
        connection delivers nothing within it, the connection is force-dropped and reconnected (it
        is alive at the TCP/ping layer but the feed has gone silent). Backoff starts at
        ``_BASE_BACKOFF_S``, doubles per disconnect (25 % jitter), caps at ``ws_max_backoff_s``, and
        resets after a healthy run (≥ ``_HEALTHY_RUN_S``).
        """
        backoff = _BASE_BACKOFF_S
        stall_timeout = self._settings.ws_stall_timeout_s

        loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                ws = self._ws_factory()
            except Exception as exc:
                # A factory that does I/O (proxy/credential handles) can fail transiently; treat it
                # like any other connect failure and back off rather than crashing the whole runner.
                log.warning("ws_factory_failed", error=repr(exc))
                await self._backoff_sleep(backoff)
                backoff = min(backoff * 2.0, self._settings.ws_max_backoff_s)
                continue
            metrics.WS_RECONNECTS.inc()
            t_start = loop.time()
            # A fresh connection re-subscribes the full current set in its handshake, so any ops
            # queued for the prior connection are obsolete — drop them to avoid spurious sends.
            self._drain_control()
            agen = ws.stream(self._token_ids, control=self._control)

            try:
                while not stop.is_set():
                    try:
                        if stall_timeout > 0:
                            msg = await asyncio.wait_for(anext(agen), timeout=stall_timeout)
                        else:
                            msg = await anext(agen)
                    except StopAsyncIteration:
                        log.info("ws_stream_ended")  # server closed cleanly
                        break
                    except TimeoutError:
                        metrics.WS_STALLS.inc()
                        log.warning("ws_stall", timeout_s=stall_timeout)
                        break  # connected but silent → reconnect + resync
                    self._ingest(msg)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await agen.aclose()
                return
            except Exception as exc:
                log.warning("ws_disconnect", error=repr(exc))
            finally:
                with contextlib.suppress(Exception):
                    await agen.aclose()

            if stop.is_set():
                return

            elapsed = loop.time() - t_start
            if elapsed >= _HEALTHY_RUN_S:
                backoff = _BASE_BACKOFF_S  # healthy run → reset
                log.debug("ws_backoff_reset")

            await self._backoff_sleep(backoff)
            backoff = min(backoff * 2.0, self._settings.ws_max_backoff_s)

    async def _backoff_sleep(self, backoff: float) -> None:
        """Sleep the current reconnect backoff plus ≤25 % jitter, capped at ws_max_backoff_s."""
        jitter = random.uniform(0.0, backoff * 0.25)
        wait_s = min(backoff + jitter, self._settings.ws_max_backoff_s)
        log.info("ws_reconnecting", delay_s=round(wait_s, 2))
        await self._sleep(wait_s)

    def _ingest(self, msg: Any) -> None:
        """Apply one WS message to the cache and stamp freshness/liveness (R2/R8)."""
        changed = self._cache.apply(msg)
        if not changed:
            return
        now = asyncio.get_running_loop().time()
        for tok in changed:
            self._last_update[tok] = now
        self._last_message_wall = heartbeat.now_epoch()
        metrics.WS_LAST_MESSAGE.set(self._last_message_wall)  # true-delta gauge (excludes resync)

    def _drain_control(self) -> None:
        """Discard any queued control ops (used on a fresh connect — see ``_stream_loop``)."""
        while True:
            try:
                self._control.get_nowait()
            except asyncio.QueueEmpty:
                return

    # ------------------------------------------------------------------
    # Internal: resync loop
    # ------------------------------------------------------------------

    async def _resync_loop(self, stop: asyncio.Event) -> None:
        """Drain stale tokens on demand + periodic full resync; refresh metrics + WS heartbeat.

        Wakes every ``_STALE_CHECK_INTERVAL_S`` seconds. Each wake: (1) drain
        ``cache.take_stale()`` and REST-fetch those tokens; (2) every ``ws_resync_interval_s``,
        REST-fetch ALL tracked tokens (full-depth backup read); (3) publish liveness metrics and
        write the WS heartbeat from the freshest of (last message, last resync). Fetch errors are
        logged and skipped — never fatal.
        """
        # -inf ⇒ the first wake ALWAYS does a full REST resync immediately (independent of the
        # monotonic clock's magnitude — on a freshly-booted host loop.time() can be < the resync
        # interval), guaranteeing a complete book set at startup (belt-and-suspenders with the WS
        # initial_dump). Thereafter it fires every interval.
        last_full_resync = float("-inf")
        loop = asyncio.get_running_loop()

        while not stop.is_set():
            await self._sleep(_STALE_CHECK_INTERVAL_S)
            if stop.is_set():
                return

            # On-demand: flush stale tokens first (known integrity problem, resync quickly).
            stale = self._cache.take_stale()
            if stale:
                log.info("resync_stale_tokens", count=len(stale))
                await self._resync_tokens(sorted(stale))

            # Periodic full resync: correct any drift the top-of-book check can't see.
            now = loop.time()
            if now - last_full_resync >= self._settings.ws_resync_interval_s:
                log.info("resync_full", token_count=len(self._token_ids))
                await self._resync_tokens(self._token_ids)
                last_full_resync = now

            self._publish_liveness()

    def _publish_liveness(self) -> None:
        """Refresh R8 metrics and write the WS heartbeat = "runner alive and maintaining books".

        The heartbeat pulses on the freshest of a live delta or a successful resync. With NO
        tracked tokens there is nothing to keep fresh (e.g. a startup discovery outage left the set
        empty) — a healthy idle state, not a frozen cache — so pulse 'now' and let a Gamma-only
        outage degrade instead of crash-looping the container. The per-source gauges
        (WS_LAST_MESSAGE / WS_LAST_RESYNC) stay separate so a monitor can still see a dead feed that
        resync is masking.
        """
        metrics.WS_TOKENS.set(self._cache.token_count)
        metrics.WS_SKIPS.set(self._cache.skip_count)
        if not self._token_ids:
            heartbeat.write(self._settings.ws_heartbeat_path, heartbeat.now_epoch())
            return
        fresh_wall = max(self._last_message_wall, self._last_resync_wall)
        if fresh_wall > 0.0:
            heartbeat.write(self._settings.ws_heartbeat_path, fresh_wall)

    async def _resync_tokens(self, tokens: Sequence[str]) -> None:
        """REST-fetch books for ``tokens`` and seed the cache (the backup read path).

        Throttled to ``_RESYNC_CONCURRENCY`` concurrent requests (the ClobClient has its own rate
        limiter; the semaphore prevents a thundering-herd burst). Each failed fetch increments
        ``WS_RESYNC_ERRORS`` and leaves the cached book unchanged; a successful one stamps freshness
        so resync keeps a token alive even while its WS feed is quiet.
        """
        if not tokens:
            return
        metrics.WS_RESYNCS.inc()
        sem = asyncio.Semaphore(_RESYNC_CONCURRENCY)
        loop = asyncio.get_running_loop()

        async def _one(token_id: str) -> None:
            async with sem:
                try:
                    book = await self._clob.get_order_book(token_id)
                    # Re-check tracking AFTER the await: a concurrent set_tokens() may have evicted
                    # this token while the fetch was in flight, and re-seeding would resurrect a
                    # zombie entry the runner already dropped (bug-hunt + committee finding).
                    # Skipping is a pure false-negative (a missed resync), never a fabricated book.
                    if token_id not in self._tracked:
                        return
                    self._cache.seed(book)
                    self._last_update[token_id] = loop.time()
                    self._last_resync_wall = heartbeat.now_epoch()
                    metrics.WS_LAST_RESYNC.set(self._last_resync_wall)
                except httpx.HTTPError as exc:
                    metrics.WS_RESYNC_ERRORS.inc()
                    log.warning("resync_fetch_error", token_id=token_id, error=repr(exc))
                except Exception as exc:
                    metrics.WS_RESYNC_ERRORS.inc()
                    log.warning("resync_unexpected_error", token_id=token_id, error=repr(exc))

        await asyncio.gather(*(_one(t) for t in tokens))
