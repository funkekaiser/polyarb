"""In-memory order-book cache for the CLOB market-channel websocket.

Turns decoded WS messages into the same ``dict[token_id, OrderBook]`` that the
detectors consume.  This module is purely stateful and offline — no I/O, no
asyncio.  Connection handling, reconnect, and scanner integration are separate.

Apply semantics
---------------
``book`` event
    REPLACE the entire book for the named ``asset_id``; update market, timestamp,
    hash, tick_size, last_trade_price.  Clears any prior stale flag for that token.

``price_change`` event
    Apply each entry in ``price_changes`` (each has its own ``asset_id``) as a
    delta.  ``side == "BUY"`` (case-insensitive) → bids; ``"SELL"`` → asks.
    Set that price level's size; if size <= 0 remove the level.  After applying,
    compare the entry's ``best_bid``/``best_ask`` (if non-empty) to the freshly
    computed best of the cache for that token — mismatch means we missed an earlier
    delta, so the token is flagged in ``stale_tokens`` for REST resync.  Delta to
    an unknown token (no prior ``book`` snapshot) is also flagged and skipped.

``tick_size_change`` / ``last_trade_price`` events
    Update the named scalar field for the token(s) named in the message; ignore
    unrecognised payload shapes gracefully.

Any other ``event_type``
    Silently ignored (returns empty changed set).

Malformed input (missing keys, unparseable numbers, non-dict in a list) is always
skipped — the cache never raises on bad data.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from polyarb.models import BookLevel, OrderBook

log = logging.getLogger(__name__)

# Max concurrent REST fetches per resync pass (consumed by the streaming runner).
RESYNC_BATCH_SIZE: int = 20

# Recent per-token book hashes retained for A3 hash-revert detection.
_HASH_HISTORY_SIZE: int = 8


def _dec(value: Any) -> Decimal | None:
    """Parse *value* to Decimal; return None on failure."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _dec_strict(value: Any) -> Decimal:
    """Parse *value* to Decimal; raise InvalidOperation on failure."""
    return Decimal(str(value))


@dataclass
class _TokenState:
    """Mutable per-token state.  Never exposed directly; only materialised as
    an ``OrderBook`` via :meth:`OrderBookCache.book`."""

    market: str
    timestamp_ms: int
    bids: dict[Decimal, Decimal] = field(default_factory=dict)  # price → size
    asks: dict[Decimal, Decimal] = field(default_factory=dict)  # price → size
    hash: str | None = None
    tick_size: Decimal | None = None
    last_trade_price: Decimal | None = None
    # Bounded history of recent book hashes for A3 hash-revert detection.
    hashes: deque[str] = field(default_factory=lambda: deque(maxlen=_HASH_HISTORY_SIZE))

    # ------------------------------------------------------------------ helpers
    def observe_hash(self, h: str) -> bool:
        """Record book hash ``h``; return True if it is a REVERT — ``h`` matches an
        EARLIER entry, i.e. the server rolled the book back to a prior snapshot (the
        #180 corrupt/replay pattern). An immediate repeat of the current last hash is
        a no-op echo, not a revert. History is bounded, so a very old (evicted) hash
        is not flagged — conservative by design.
        """
        if self.hashes:
            if h == self.hashes[-1]:
                return False  # immediate echo of the same state — not a revert
            if h in self.hashes:
                self.hashes.append(h)
                return True
        self.hashes.append(h)
        return False

    def best_bid_price(self) -> Decimal | None:
        valid = [p for p, s in self.bids.items() if p > 0 and s > 0]
        return max(valid) if valid else None

    def best_ask_price(self) -> Decimal | None:
        valid = [p for p, s in self.asks.items() if p > 0 and s > 0]
        return min(valid) if valid else None


class OrderBookCache:
    """Stateful in-memory cache: WS messages in, ``OrderBook`` objects out.

    Parameters
    ----------
    skip_count:
        After construction, ``cache.skip_count`` tracks how many individual
        delta/event entries were skipped due to missing keys or parse errors.
    stale_tokens:
        Set of token_ids that have been flagged for REST resync.  Use
        :meth:`take_stale` to retrieve and clear atomically.
    """

    def __init__(self) -> None:
        self._state: dict[str, _TokenState] = {}
        self._stale: set[str] = set()
        self.skip_count: int = 0

    # ------------------------------------------------------------------ public
    def apply(self, message: Any) -> set[str]:
        """Ingest one decoded WS message (dict *or* list of dicts).

        Returns the set of ``token_id``\\s whose book changed.  Never raises on
        malformed input — bad entries are skipped and counted in
        :attr:`skip_count`.
        """
        if isinstance(message, list):
            events = message
        elif isinstance(message, dict):
            events = [message]
        else:
            return set()

        changed: set[str] = set()
        for event in events:
            if not isinstance(event, dict):
                self.skip_count += 1
                continue
            event_type = event.get("event_type", "")
            if event_type == "book":
                tok = self._handle_book(event)
                if tok:
                    changed.add(tok)
            elif event_type == "price_change":
                changed |= self._handle_price_change(event)
            elif event_type == "tick_size_change":
                tok = self._handle_tick_size_change(event)
                if tok:
                    changed.add(tok)
            elif event_type == "last_trade_price":
                tok = self._handle_last_trade_price(event)
                if tok:
                    changed.add(tok)
            # else: unknown event_type — silently ignore

        return changed

    def book(self, token_id: str) -> OrderBook | None:
        """Materialise the current book for *token_id*, or ``None`` if unknown."""
        state = self._state.get(token_id)
        if state is None:
            return None
        return self._materialise(token_id, state)

    def books(self) -> dict[str, OrderBook]:
        """Materialise all known books."""
        return {tid: self._materialise(tid, st) for tid, st in self._state.items()}

    def take_stale(self) -> set[str]:
        """Return and clear the set of tokens flagged for REST resync."""
        stale = self._stale.copy()
        self._stale.clear()
        return stale

    @property
    def stale_tokens(self) -> frozenset[str]:
        """Read-only view of the current stale set (does NOT clear it)."""
        return frozenset(self._stale)

    def seed(self, book: OrderBook) -> None:
        """Replace a token's state from a full REST ``OrderBook`` — the resync entry point.

        The streaming runner's REST safety net calls this to correct full-depth drift that the
        top-of-book WS integrity check can't catch. Clears the token's stale flag (the REST
        snapshot is authoritative) and PRESERVES the WS hash history (REST hashes are not part
        of the WS sequence, so they must not perturb revert detection). Non-positive levels are
        dropped, matching the validity filter used everywhere else.
        """
        bids = {lvl.price: lvl.size for lvl in book.bids if lvl.price > 0 and lvl.size > 0}
        asks = {lvl.price: lvl.size for lvl in book.asks if lvl.price > 0 and lvl.size > 0}
        state = _TokenState(
            market=book.market,
            timestamp_ms=book.timestamp_ms,
            bids=bids,
            asks=asks,
            hash=book.hash,
            tick_size=book.tick_size,
            last_trade_price=book.last_trade_price,
        )
        existing = self._state.get(book.asset_id)
        if existing is not None:
            state.hashes = existing.hashes
        self._state[book.asset_id] = state
        self._stale.discard(book.asset_id)

    # ---------------------------------------------------------- event handlers
    def _handle_book(self, event: dict[str, Any]) -> str | None:
        """Full snapshot replacement.  Returns token_id on success."""
        asset_id = event.get("asset_id")
        market = event.get("market")
        if not asset_id or not market:
            self.skip_count += 1
            return None

        ts_raw = event.get("timestamp", 0)
        try:
            ts = int(float(str(ts_raw)))
        except (ValueError, TypeError):
            ts = 0

        bids: dict[Decimal, Decimal] = {}
        for level in event.get("bids", []):
            p = _dec(level.get("price"))
            s = _dec(level.get("size"))
            if p is not None and s is not None and p > 0 and s > 0:
                bids[p] = s

        asks: dict[Decimal, Decimal] = {}
        for level in event.get("asks", []):
            p = _dec(level.get("price"))
            s = _dec(level.get("size"))
            if p is not None and s is not None and p > 0 and s > 0:
                asks[p] = s

        state = _TokenState(
            market=market,
            timestamp_ms=ts,
            bids=bids,
            asks=asks,
            hash=event.get("hash") or None,
            tick_size=_dec(event.get("tick_size")),
            last_trade_price=_dec(event.get("last_trade_price")),
        )
        # Carry the hash history across the snapshot replacement so a revert to an
        # earlier snapshot stays detectable (A3 hash-revert).
        existing = self._state.get(asset_id)
        if existing is not None:
            state.hashes = existing.hashes
        self._state[asset_id] = state
        # A fresh snapshot clears any prior stale flag...
        self._stale.discard(asset_id)
        # ...but a hash that reverts to an earlier snapshot re-flags it for resync.
        h = event.get("hash")
        if h and state.observe_hash(str(h)):
            self._stale.add(asset_id)
        return str(asset_id)

    def _handle_price_change(self, event: dict[str, Any]) -> set[str]:
        """Delta apply.  Returns set of changed token_ids."""
        ts_raw = event.get("timestamp", 0)
        try:
            ts = int(float(str(ts_raw)))
        except (ValueError, TypeError):
            ts = 0

        changed: set[str] = set()
        for entry in event.get("price_changes", []):
            if not isinstance(entry, dict):
                self.skip_count += 1
                continue

            asset_id = entry.get("asset_id")
            if not asset_id:
                self.skip_count += 1
                continue

            # Delta to an unknown book — flag stale, skip.
            if asset_id not in self._state:
                self._stale.add(asset_id)
                continue

            state = self._state[asset_id]

            # Parse price and size.
            price = _dec(entry.get("price"))
            size = _dec(entry.get("size"))
            if price is None or size is None:
                self.skip_count += 1
                continue

            # Determine which side.
            side_raw = str(entry.get("side", "")).upper()
            if side_raw == "BUY":
                book_side = state.bids
            elif side_raw == "SELL":
                book_side = state.asks
            else:
                self.skip_count += 1
                continue

            # Apply delta: set or remove the level.
            if size <= 0:
                book_side.pop(price, None)
            else:
                book_side[price] = size

            # Update timestamp and hash; a hash-revert flags the token for resync (A3).
            state.timestamp_ms = ts
            entry_hash = entry.get("hash")
            if entry_hash:
                state.hash = str(entry_hash)
                if state.observe_hash(str(entry_hash)):
                    self._stale.add(asset_id)

            # Integrity check: compare declared best_bid/best_ask to computed.
            declared_bb_raw = entry.get("best_bid", "")
            declared_ba_raw = entry.get("best_ask", "")
            if declared_bb_raw != "" or declared_ba_raw != "":
                declared_bb = _dec(declared_bb_raw) if declared_bb_raw not in ("", None) else None
                declared_ba = _dec(declared_ba_raw) if declared_ba_raw not in ("", None) else None
                computed_bb = state.best_bid_price()
                computed_ba = state.best_ask_price()
                mismatch = False
                if declared_bb is not None and computed_bb != declared_bb:
                    mismatch = True
                if declared_ba is not None and computed_ba != declared_ba:
                    mismatch = True
                if mismatch:
                    self._stale.add(asset_id)

            changed.add(asset_id)

        return changed

    def _handle_tick_size_change(self, event: dict[str, Any]) -> str | None:
        """Update tick_size for the named asset_id."""
        asset_id = event.get("asset_id")
        if not asset_id or asset_id not in self._state:
            return None
        ts = _dec(event.get("tick_size"))
        if ts is not None:
            self._state[asset_id].tick_size = ts
        return str(asset_id)

    def _handle_last_trade_price(self, event: dict[str, Any]) -> str | None:
        """Update last_trade_price for the named asset_id."""
        asset_id = event.get("asset_id")
        if not asset_id or asset_id not in self._state:
            return None
        ltp = _dec(event.get("last_trade_price"))
        if ltp is not None:
            self._state[asset_id].last_trade_price = ltp
        return str(asset_id)

    # ---------------------------------------------------------- materialisation
    def _materialise(self, asset_id: str, state: _TokenState) -> OrderBook:
        """Build an ``OrderBook`` from internal mutable state.

        Levels are sorted deterministically (bids descending, asks ascending) and
        any non-positive price/size levels are excluded — consistent with the
        ``best_bid``/``best_ask`` validity filter on ``OrderBook``.
        """
        bids = sorted(
            (BookLevel(price=p, size=s) for p, s in state.bids.items() if p > 0 and s > 0),
            key=lambda lvl: lvl.price,
            reverse=True,
        )
        asks = sorted(
            (BookLevel(price=p, size=s) for p, s in state.asks.items() if p > 0 and s > 0),
            key=lambda lvl: lvl.price,
        )
        return OrderBook(
            market=state.market,
            asset_id=asset_id,
            timestamp=state.timestamp_ms,
            bids=bids,
            asks=asks,
            tick_size=state.tick_size,
            last_trade_price=state.last_trade_price,
            hash=state.hash,
        )
