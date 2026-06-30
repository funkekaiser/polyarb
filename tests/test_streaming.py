"""Offline tests for StreamingBooks (engine/streaming.py).

All tests use:
- FakeWS      — scripted async generator; yields messages, then raises or returns.
- FakeClob    — records get_order_book calls; returns pre-supplied books or raises.
- Injected sleep — asyncio.sleep(0) for instant backoff so tests complete fast.
- Injected stop event — controls when run() returns.

Async functions are wrapped in asyncio.run() (the pattern used throughout this
test suite; no pytest-asyncio dependency required).

No real network calls are made.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any

import httpx
import pytest

from polyarb.config import Settings
from polyarb.engine.bookcache import OrderBookCache
from polyarb.engine.streaming import StreamingBooks
from polyarb.models import BookLevel, OrderBook

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    """Minimal Settings for tests (fast backoff, fast resync)."""
    defaults: dict[str, Any] = {
        "ws_resync_interval_s": 0.001,  # effectively immediate in test time
        "ws_max_backoff_s": 0.001,  # tiny backoff so reconnect is instant
        "streaming_enabled": True,
    }
    return Settings(**{**defaults, **overrides})


def _book(
    asset_id: str = "tok1",
    *,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
) -> OrderBook:
    return OrderBook(
        market="0xM",
        asset_id=asset_id,
        timestamp_ms=1000,
        bids=[BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (bids or [])],
        asks=[BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (asks or [])],
    )


def _book_msg(
    asset_id: str = "tok1",
    *,
    hash_: str | None = None,
    bids: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "event_type": "book",
        "asset_id": asset_id,
        "market": "0xM",
        "timestamp": "1000",
        "bids": [{"price": p, "size": s} for p, s in (bids or [("0.5", "100")])],
        "asks": [],
    }
    if hash_ is not None:
        msg["hash"] = hash_
    return msg


class FakeWS:
    """A fake MarketWebSocket whose stream() yields scripted messages.

    ``on_stream_end`` is called (if supplied) right before the generator
    either returns normally or raises ``error``.  This hook lets the test
    signal the ``stop`` event at a precise moment.
    """

    def __init__(
        self,
        messages: list[dict[str, Any]],
        *,
        error: Exception | None = None,
        on_stream_end: Any = None,
        hang_after: bool = False,
    ) -> None:
        self._messages = messages
        self._error = error
        self._on_stream_end = on_stream_end
        self._hang_after = hang_after

    async def stream(
        self,
        token_ids: Sequence[str],
        *,
        control: Any = None,
    ) -> AsyncIterator[dict[str, Any]]:
        for msg in self._messages:
            yield msg
        if self._on_stream_end is not None:
            self._on_stream_end()
        if self._hang_after:
            await asyncio.Event().wait()  # open but silent — exercises the R5 stall watchdog
        if self._error is not None:
            raise self._error


class FakeClob:
    """Records get_order_book calls; returns pre-supplied books or raises.

    ``on_call`` is invoked once on the FIRST call to ``get_order_book``, before
    the book is returned or an error raised.  Tests use it to set the stop
    event so the runner exits after the first resync completes.
    """

    def __init__(
        self,
        books: dict[str, OrderBook] | None = None,
        *,
        error: Exception | None = None,
        on_call: Any = None,
    ) -> None:
        self._books = books or {}
        self._error = error
        self._on_call = on_call
        self._on_call_fired = False
        self.calls: list[str] = []

    async def get_order_book(self, token_id: str) -> OrderBook:
        self.calls.append(token_id)
        # Fire on_call once (first call only) — typically sets the stop event.
        if self._on_call is not None and not self._on_call_fired:
            self._on_call_fired = True
            self._on_call()
        if self._error is not None:
            raise self._error
        book = self._books.get(token_id)
        if book is None:
            raise httpx.HTTPStatusError(
                f"404 {token_id}",
                request=httpx.Request("GET", "/book"),
                response=httpx.Response(404),
            )
        return book


def _make_runner(
    token_ids: list[str],
    *,
    ws_factory: Any,
    clob: Any = None,
    cache: OrderBookCache | None = None,
    settings: Settings | None = None,
) -> StreamingBooks:
    return StreamingBooks(
        token_ids,
        clob=clob or FakeClob(),  # type: ignore[arg-type]
        settings=settings or _settings(),
        ws_factory=ws_factory,
        cache=cache,
        sleep=lambda t: asyncio.sleep(0),  # instant backoff
    )


# ---------------------------------------------------------------------------
# Test: messages applied to cache
# ---------------------------------------------------------------------------


def test_messages_applied_to_cache() -> None:
    """WS messages are applied to the cache; books() returns populated books."""

    async def go() -> None:
        stop = asyncio.Event()

        def factory() -> FakeWS:
            def on_end() -> None:
                stop.set()

            return FakeWS(
                [_book_msg("tok1", bids=[("0.5", "100")])],
                on_stream_end=on_end,
            )

        cache = OrderBookCache()
        runner = _make_runner(["tok1"], ws_factory=factory, cache=cache)
        await asyncio.wait_for(runner.run(stop), timeout=5.0)

        books = runner.books()
        assert "tok1" in books
        book = books["tok1"]
        assert book.best_bid is not None
        assert book.best_bid.price == Decimal("0.5")

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Test: reconnect on disconnect
# ---------------------------------------------------------------------------


def test_reconnects_on_disconnect() -> None:
    """Runner reconnects (calls ws_factory again) after a WS disconnect."""

    async def go() -> None:
        factory_log: list[int] = []
        stop = asyncio.Event()

        def factory() -> FakeWS:
            n = len(factory_log) + 1
            factory_log.append(n)

            def on_end() -> None:
                if n >= 3:
                    stop.set()

            return FakeWS(
                [_book_msg("tok1")],
                error=ConnectionResetError("simulated disconnect"),
                on_stream_end=on_end,
            )

        runner = _make_runner(["tok1"], ws_factory=factory)
        await asyncio.wait_for(runner.run(stop), timeout=5.0)

        # ws_factory must have been called more than once (reconnected at least twice)
        assert len(factory_log) >= 3, f"expected ≥3 factory calls, got {factory_log}"

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Test: no crash after healthy stream ends normally (no error raised)
# ---------------------------------------------------------------------------


def test_reconnects_after_clean_stream_end() -> None:
    """Runner reconnects even when the WS stream ends without raising."""

    async def go() -> None:
        factory_log: list[int] = []
        stop = asyncio.Event()

        def factory() -> FakeWS:
            n = len(factory_log) + 1
            factory_log.append(n)

            def on_end() -> None:
                if n >= 2:
                    stop.set()

            # No error — stream just ends cleanly
            return FakeWS([_book_msg("tok1")], on_stream_end=on_end)

        runner = _make_runner(["tok1"], ws_factory=factory)
        await asyncio.wait_for(runner.run(stop), timeout=5.0)
        assert len(factory_log) >= 2

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Test: stop event → returns promptly
# ---------------------------------------------------------------------------


def test_stop_returns_promptly() -> None:
    """Setting the stop event causes run() to return cleanly."""

    async def go() -> None:
        stop = asyncio.Event()
        stop.set()  # stop before run even starts

        def factory() -> FakeWS:
            return FakeWS([])  # no messages; stream ends immediately

        runner = _make_runner(["tok1"], ws_factory=factory)
        # Should complete immediately; 1-second timeout is generous
        await asyncio.wait_for(runner.run(stop), timeout=1.0)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Test: periodic resync calls seed
# ---------------------------------------------------------------------------


def test_periodic_resync_seeds_cache() -> None:
    """Resync loop calls clob.get_order_book and seeds the cache.

    The WS stream raises after yielding messages so the stream loop hits its
    backoff sleep — that sleep is the first point where the event loop can
    schedule the resync task.  The clob's on_call sets stop on the first
    get_order_book call so the runner exits promptly.
    """

    async def go() -> FakeClob:
        stop = asyncio.Event()

        def factory() -> FakeWS:
            # Raise after messages so stream loop hits backoff sleep, yielding
            # to the event loop and allowing the resync task to execute.
            return FakeWS(
                [_book_msg("tok1")],
                error=ConnectionResetError("test disconnect"),
            )

        clob = FakeClob(
            {"tok1": _book("tok1", bids=[("0.8", "999")])},
            # Set stop on first resync call so the runner exits.
            on_call=stop.set,
        )
        runner = _make_runner(
            ["tok1"],
            ws_factory=factory,
            clob=clob,
            # ws_resync_interval_s=0 → full resync fires immediately on first
            # resync loop iteration (last_full_resync=0, now >> 0).
            settings=_settings(ws_resync_interval_s=0.0),
        )

        await asyncio.wait_for(runner.run(stop), timeout=5.0)
        return clob

    clob = asyncio.run(go())
    # At least one resync call must have been made
    assert "tok1" in clob.calls


# ---------------------------------------------------------------------------
# Test: resync fetch error is not fatal
# ---------------------------------------------------------------------------


def test_resync_fetch_error_not_fatal() -> None:
    """An httpx.HTTPError during resync is logged and skipped; run continues.

    The WS raises to give the resync task event-loop time.  The clob always
    raises httpx.HTTPError; the on_call callback sets stop so the runner exits
    after the (failed) resync attempt, proving the error was swallowed.
    """

    async def go() -> FakeClob:
        stop = asyncio.Event()

        def factory() -> FakeWS:
            return FakeWS(
                [_book_msg("tok1")],
                error=ConnectionResetError("test disconnect"),
            )

        clob = FakeClob(
            error=httpx.HTTPStatusError(
                "500 server error",
                request=httpx.Request("GET", "/book"),
                response=httpx.Response(500),
            ),
            on_call=stop.set,  # set stop after (failed) resync attempt
        )
        runner = _make_runner(
            ["tok1"],
            ws_factory=factory,
            clob=clob,
            settings=_settings(ws_resync_interval_s=0.0),
        )

        # Must complete without raising
        await asyncio.wait_for(runner.run(stop), timeout=5.0)
        return clob

    clob = asyncio.run(go())
    # Resync was attempted (error was swallowed, not propagated)
    assert "tok1" in clob.calls


# ---------------------------------------------------------------------------
# Test: stale tokens are resynced on demand
# ---------------------------------------------------------------------------


def test_stale_tokens_resynced_on_demand() -> None:
    """Tokens flagged stale (e.g. by hash-revert) are REST-fetched promptly.

    The hash sequence A→B→A triggers A3 hash-revert detection, flagging tok1
    stale.  The WS then raises so the stream loop sleeps (yielding to the event
    loop), the resync task drains the stale set, and the clob's on_call stops
    the runner.
    """

    async def go() -> FakeClob:
        stop = asyncio.Event()

        def factory() -> FakeWS:
            # Three messages that trigger A3 hash revert: A → B → A
            msgs = [
                _book_msg("tok1", hash_="A"),
                _book_msg("tok1", hash_="B"),
                _book_msg("tok1", hash_="A"),  # revert → tok1 goes stale
            ]
            return FakeWS(msgs, error=ConnectionResetError("test disconnect"))

        clob = FakeClob(
            {"tok1": _book("tok1")},
            on_call=stop.set,  # set stop once the stale resync fires
        )
        runner = _make_runner(
            ["tok1"],
            ws_factory=factory,
            clob=clob,
            settings=_settings(ws_resync_interval_s=9999.0),  # disable periodic full resync
        )

        await asyncio.wait_for(runner.run(stop), timeout=5.0)
        return clob

    clob = asyncio.run(go())
    # The stale token must have been REST-fetched at least once
    assert "tok1" in clob.calls, "stale tok1 was not resynced"


# ---------------------------------------------------------------------------
# Test: cancellation cleans up
# ---------------------------------------------------------------------------


def test_cancellation_cleans_up() -> None:
    """CancelledError on the run task propagates cleanly without hanging."""

    async def go() -> None:
        stop = asyncio.Event()

        def factory() -> FakeWS:
            # Stream never ends on its own (no messages, no error)
            return FakeWS([])

        runner = _make_runner(["tok1"], ws_factory=factory)
        task = asyncio.create_task(runner.run(stop))
        # Give the task a moment to start
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(go(), timeout=2.0))


# ---------------------------------------------------------------------------
# Test: books() reflects cache state
# ---------------------------------------------------------------------------


def test_books_reflects_latest_cache() -> None:
    """StreamingBooks.books() is a live snapshot of the underlying cache."""

    async def go() -> StreamingBooks:
        stop = asyncio.Event()

        def factory() -> FakeWS:
            def on_end() -> None:
                stop.set()

            return FakeWS(
                [
                    _book_msg("tok1", bids=[("0.5", "100")]),
                    _book_msg("tok2", bids=[("0.3", "50")]),
                ],
                on_stream_end=on_end,
            )

        runner = _make_runner(["tok1", "tok2"], ws_factory=factory)
        await asyncio.wait_for(runner.run(stop), timeout=5.0)
        return runner

    runner = asyncio.run(go())
    books = runner.books()
    assert set(books.keys()) == {"tok1", "tok2"}
    assert books["tok1"].best_bid is not None
    assert books["tok2"].best_bid is not None


# ---------------------------------------------------------------------------
# Test: resync non-httpx error also swallowed
# ---------------------------------------------------------------------------


def test_resync_unexpected_error_not_fatal() -> None:
    """A non-httpx exception during resync is also logged and skipped."""

    async def go() -> list[str]:
        stop = asyncio.Event()
        calls: list[str] = []

        def factory() -> FakeWS:
            return FakeWS(
                [_book_msg("tok1")],
                error=ConnectionResetError("test disconnect"),
            )

        class BrokenClob:
            async def get_order_book(self, token_id: str) -> OrderBook:
                calls.append(token_id)
                stop.set()  # stop after first call
                raise RuntimeError("unexpected error in clob")

        runner = _make_runner(
            ["tok1"],
            ws_factory=factory,
            clob=BrokenClob(),  # type: ignore[arg-type]
            settings=_settings(ws_resync_interval_s=0.0),
        )

        # Must complete without raising
        await asyncio.wait_for(runner.run(stop), timeout=5.0)
        return calls

    calls = asyncio.run(go())
    assert calls  # resync was attempted


def test_set_tokens_evicts_dropped_tokens() -> None:
    """R6 (partial): updating the token set evicts books for tokens that left discovery."""
    cache = OrderBookCache()
    cache.seed(_book("old"))
    cache.seed(_book("keep"))
    sb = StreamingBooks(
        ["old", "keep"],
        clob=FakeClob({}),  # type: ignore[arg-type]
        settings=_settings(),
        cache=cache,
    )
    sb.set_tokens(["keep", "new"])
    assert "old" not in cache.books()  # dropped from discovery → evicted
    assert "keep" in cache.books()  # still tracked → retained
    sb.set_tokens(["new", "keep"])  # same set, different order → idempotent, no error


# ---------------------------------------------------------------------------
# R5 — stall watchdog: a connected-but-silent feed is force-reconnected
# ---------------------------------------------------------------------------


def test_stall_watchdog_forces_reconnect() -> None:
    """A WS that yields then goes silent past ws_stall_timeout_s is dropped + reconnected (R5)."""
    from polyarb.engine import metrics

    async def go() -> int:
        factory_log: list[int] = []
        stop = asyncio.Event()

        def factory() -> FakeWS:
            n = len(factory_log) + 1
            factory_log.append(n)
            if n >= 2:
                stop.set()  # exit after the watchdog forced a second connection
            # First connection: yield one msg then hang (open but silent) → stall.
            return FakeWS([_book_msg("tok1")], hang_after=True)

        runner = _make_runner(
            ["tok1"],
            ws_factory=factory,
            settings=_settings(ws_stall_timeout_s=0.05, ws_resync_interval_s=9999.0),
        )
        before = metrics.WS_STALLS._value.get()  # type: ignore[attr-defined]
        await asyncio.wait_for(runner.run(stop), timeout=5.0)
        return int(metrics.WS_STALLS._value.get() - before)  # type: ignore[attr-defined]

    stalls = asyncio.run(go())
    assert stalls >= 1, "stall watchdog must have fired at least once"


# ---------------------------------------------------------------------------
# R2 — fresh_books drops feed-silent tokens
# ---------------------------------------------------------------------------


def test_fresh_books_filters_stale_tokens() -> None:
    """fresh_books returns only tokens refreshed within the window; unstamped/old ones drop."""

    async def go() -> dict[str, Any]:
        cache = OrderBookCache()
        cache.seed(_book("stale", bids=[("0.4", "100")]))  # seeded directly → never stamped
        runner = _make_runner(["fresh", "stale"], cache=cache, ws_factory=lambda: FakeWS([]))
        runner._ingest(_book_msg("fresh", bids=[("0.5", "100")]))  # stamps 'fresh' at loop.time()
        return {
            "all": set(runner.books()),
            "fresh_only": set(runner.fresh_books(30.0)),
            "disabled": set(runner.fresh_books(0.0)),
        }

    out = asyncio.run(go())
    assert out["all"] == {"fresh", "stale"}
    assert out["fresh_only"] == {"fresh"}  # 'stale' has no recent update → dropped
    assert out["disabled"] == {"fresh", "stale"}  # window<=0 disables the guard


# ---------------------------------------------------------------------------
# R6 — set_tokens enqueues subscribe/unsubscribe ops for the live connection
# ---------------------------------------------------------------------------


def test_set_tokens_enqueues_dynamic_sub_ops() -> None:
    """Adding/dropping tokens queues subscribe/unsubscribe ops (R6, dynamic resubscribe)."""
    sb = StreamingBooks(
        ["a", "b"],
        clob=FakeClob({}),  # type: ignore[arg-type]
        settings=_settings(),
    )
    sb.set_tokens(["b", "c"])  # +c, -a
    ops = []
    while not sb._control.empty():
        ops.append(sb._control.get_nowait())
    assert {"operation": "subscribe", "assets_ids": ["c"]} in ops
    assert {"operation": "unsubscribe", "assets_ids": ["a"]} in ops


# ---------------------------------------------------------------------------
# R8 — resync/message liveness pulses the WS heartbeat file
# ---------------------------------------------------------------------------


def test_publish_liveness_writes_ws_heartbeat(tmp_path: Any) -> None:
    """_publish_liveness writes the freshest message/resync epoch to ws_heartbeat_path (R8)."""
    import time

    hb = tmp_path / "ws-heartbeat"
    sb = StreamingBooks(
        ["a"],
        clob=FakeClob({}),  # type: ignore[arg-type]
        settings=_settings(ws_heartbeat_path=hb),
    )
    # Nothing applied yet → no write (don't certify liveness before the first refresh).
    sb._publish_liveness()
    assert not hb.exists()
    # Simulate a successful resync, then publish → heartbeat written with a recent timestamp.
    sb._last_resync_wall = time.time()
    sb._publish_liveness()
    assert hb.exists()
    assert abs(float(hb.read_text().strip()) - time.time()) < 5.0
