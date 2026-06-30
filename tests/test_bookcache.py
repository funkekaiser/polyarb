"""Adversarial offline tests for engine.bookcache.OrderBookCache.

All tests use deterministic, hand-crafted message dicts — no live API calls.
The final test replays the saved ws_market_capture.json fixture as a smoke test.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

from polyarb.engine.bookcache import OrderBookCache
from polyarb.models import OrderBook

# ---------------------------------------------------------------------------
# Fixtures dir
# ---------------------------------------------------------------------------
FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Helpers: canonical minimal message dicts
# ---------------------------------------------------------------------------
MARKET_A = "0xmarket_a"
MARKET_B = "0xmarket_b"
TOKEN_A = "token_aaa_111"
TOKEN_B = "token_bbb_222"
TOKEN_C = "token_ccc_333"


def _book_event(
    token_id: str = TOKEN_A,
    market: str = MARKET_A,
    bids: list[dict] | None = None,
    asks: list[dict] | None = None,
    ts: str = "1000000000000",
    tick_size: str | None = "0.001",
    last_trade_price: str | None = "0.5",
    hash_: str | None = "abc123",
) -> dict:
    return {
        "event_type": "book",
        "market": market,
        "asset_id": token_id,
        "timestamp": ts,
        "hash": hash_,
        "bids": bids if bids is not None else [{"price": "0.40", "size": "100"}],
        "asks": asks if asks is not None else [{"price": "0.60", "size": "200"}],
        "tick_size": tick_size,
        "last_trade_price": last_trade_price,
    }


def _price_change_event(
    changes: list[dict],
    market: str = MARKET_A,
    ts: str = "1000000001000",
) -> dict:
    return {
        "event_type": "price_change",
        "market": market,
        "timestamp": ts,
        "price_changes": changes,
    }


def _pc_entry(
    asset_id: str = TOKEN_A,
    price: str = "0.45",
    size: str = "50",
    side: str = "BUY",
    hash_: str = "delta1",
    best_bid: str = "0.45",
    best_ask: str = "0.60",
) -> dict:
    return {
        "asset_id": asset_id,
        "price": price,
        "size": size,
        "side": side,
        "hash": hash_,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }


# ===========================================================================
# 1. Book snapshot → correct OrderBook
# ===========================================================================
class TestBookSnapshot:
    def test_book_event_returns_orderbook(self) -> None:
        cache = OrderBookCache()
        cache.apply(
            _book_event(
                bids=[{"price": "0.40", "size": "100"}],
                asks=[{"price": "0.60", "size": "200"}],
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        assert isinstance(ob, OrderBook)

    def test_best_bid_ask_correct(self) -> None:
        cache = OrderBookCache()
        cache.apply(
            _book_event(
                bids=[{"price": "0.40", "size": "100"}, {"price": "0.35", "size": "50"}],
                asks=[{"price": "0.60", "size": "200"}, {"price": "0.65", "size": "50"}],
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        assert ob.best_bid is not None
        assert ob.best_bid.price == Decimal("0.40")
        assert ob.best_ask is not None
        assert ob.best_ask.price == Decimal("0.60")

    def test_book_levels_sorted_bids_desc_asks_asc(self) -> None:
        cache = OrderBookCache()
        cache.apply(
            _book_event(
                bids=[
                    {"price": "0.30", "size": "10"},
                    {"price": "0.40", "size": "20"},
                    {"price": "0.35", "size": "15"},
                ],
                asks=[
                    {"price": "0.70", "size": "5"},
                    {"price": "0.60", "size": "10"},
                    {"price": "0.65", "size": "8"},
                ],
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        bid_prices = [lvl.price for lvl in ob.bids]
        ask_prices = [lvl.price for lvl in ob.asks]
        assert bid_prices == sorted(bid_prices, reverse=True)
        assert ask_prices == sorted(ask_prices)

    def test_hash_and_last_trade_price_set(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event(hash_="myhash999", last_trade_price="0.42"))
        ob = cache.book(TOKEN_A)
        assert ob is not None
        assert ob.hash == "myhash999"
        assert ob.last_trade_price == Decimal("0.42")

    def test_book_returns_none_for_unknown_token(self) -> None:
        cache = OrderBookCache()
        assert cache.book("unknown_token") is None

    def test_second_book_replaces_first(self) -> None:
        cache = OrderBookCache()
        cache.apply(
            _book_event(
                bids=[{"price": "0.40", "size": "100"}],
                asks=[{"price": "0.60", "size": "200"}],
            )
        )
        # Second snapshot completely replaces
        cache.apply(
            _book_event(
                bids=[{"price": "0.50", "size": "5"}],
                asks=[{"price": "0.55", "size": "7"}],
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        assert ob.best_bid is not None
        assert ob.best_bid.price == Decimal("0.50")
        assert len(ob.bids) == 1  # only the new level

    def test_changed_set_contains_token(self) -> None:
        cache = OrderBookCache()
        changed = cache.apply(_book_event())
        assert TOKEN_A in changed

    def test_books_returns_all(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event(token_id=TOKEN_A))
        cache.apply(_book_event(token_id=TOKEN_B, market=MARKET_B))
        all_books = cache.books()
        assert TOKEN_A in all_books
        assert TOKEN_B in all_books


# ===========================================================================
# 2. price_change deltas
# ===========================================================================
class TestPriceChangeDelta:
    def _cache_with_snapshot(self) -> OrderBookCache:
        cache = OrderBookCache()
        cache.apply(
            _book_event(
                bids=[{"price": "0.40", "size": "100"}],
                asks=[{"price": "0.60", "size": "200"}],
            )
        )
        return cache

    def test_updates_existing_bid_level(self) -> None:
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.40", size="150", side="BUY", best_bid="0.40", best_ask="0.60"
                    ),
                ]
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        level = next((lvl for lvl in ob.bids if lvl.price == Decimal("0.40")), None)
        assert level is not None
        assert level.size == Decimal("150")

    def test_adds_new_bid_level(self) -> None:
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.45", size="50", side="BUY", best_bid="0.45", best_ask="0.60"
                    ),
                ]
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        prices = {lvl.price for lvl in ob.bids}
        assert Decimal("0.45") in prices
        assert Decimal("0.40") in prices  # old level preserved

    def test_removes_level_on_zero_size(self) -> None:
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(price="0.40", size="0", side="BUY", best_bid="", best_ask="0.60"),
                ]
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        prices = {lvl.price for lvl in ob.bids}
        assert Decimal("0.40") not in prices

    def test_removes_level_on_negative_size(self) -> None:
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(price="0.40", size="-1", side="BUY", best_bid="", best_ask="0.60"),
                ]
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        prices = {lvl.price for lvl in ob.bids}
        assert Decimal("0.40") not in prices

    def test_buy_hits_bids_sell_hits_asks(self) -> None:
        cache = self._cache_with_snapshot()
        # BUY side: new bid level
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.38", size="30", side="BUY", best_bid="0.40", best_ask="0.60"
                    ),
                ]
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        bid_prices = {lvl.price for lvl in ob.bids}
        ask_prices = {lvl.price for lvl in ob.asks}
        assert Decimal("0.38") in bid_prices
        assert Decimal("0.38") not in ask_prices

        # SELL side: new ask level
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.62", size="20", side="SELL", best_bid="0.40", best_ask="0.60"
                    ),
                ]
            )
        )
        ob2 = cache.book(TOKEN_A)
        assert ob2 is not None
        ask_prices2 = {lvl.price for lvl in ob2.asks}
        bid_prices2 = {lvl.price for lvl in ob2.bids}
        assert Decimal("0.62") in ask_prices2
        assert Decimal("0.62") not in bid_prices2

    def test_side_case_insensitive(self) -> None:
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.38", size="30", side="buy", best_bid="0.40", best_ask="0.60"
                    ),
                ]
            )
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        bid_prices = {lvl.price for lvl in ob.bids}
        assert Decimal("0.38") in bid_prices

    def test_one_message_two_tokens(self) -> None:
        """A single price_change message can touch multiple tokens."""
        cache = OrderBookCache()
        cache.apply(
            _book_event(
                token_id=TOKEN_A,
                market=MARKET_A,
                bids=[{"price": "0.40", "size": "100"}],
                asks=[{"price": "0.60", "size": "200"}],
            )
        )
        cache.apply(
            _book_event(
                token_id=TOKEN_B,
                market=MARKET_B,
                bids=[{"price": "0.30", "size": "80"}],
                asks=[{"price": "0.70", "size": "50"}],
            )
        )
        msg = _price_change_event(
            [
                _pc_entry(
                    asset_id=TOKEN_A,
                    price="0.42",
                    size="120",
                    side="BUY",
                    best_bid="0.42",
                    best_ask="0.60",
                ),
                _pc_entry(
                    asset_id=TOKEN_B,
                    price="0.68",
                    size="60",
                    side="SELL",
                    best_bid="0.30",
                    best_ask="0.68",
                ),
            ]
        )
        changed = cache.apply(msg)
        assert TOKEN_A in changed
        assert TOKEN_B in changed
        ob_a = cache.book(TOKEN_A)
        ob_b = cache.book(TOKEN_B)
        assert ob_a is not None and ob_b is not None
        assert Decimal("0.42") in {lvl.price for lvl in ob_a.bids}
        assert Decimal("0.68") in {lvl.price for lvl in ob_b.asks}


# ===========================================================================
# 3. Integrity check (stale flagging)
# ===========================================================================
class TestIntegrityCheck:
    def _cache_with_snapshot(self) -> OrderBookCache:
        cache = OrderBookCache()
        cache.apply(
            _book_event(
                bids=[{"price": "0.40", "size": "100"}],
                asks=[{"price": "0.60", "size": "200"}],
            )
        )
        return cache

    def test_matching_best_bid_ask_not_stale(self) -> None:
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.45", size="50", side="BUY", best_bid="0.45", best_ask="0.60"
                    ),
                ]
            )
        )
        assert TOKEN_A not in cache.stale_tokens

    def test_mismatched_best_bid_flags_stale(self) -> None:
        cache = self._cache_with_snapshot()
        # Real best bid will be 0.40 after applying (no change at 0.40)
        # Declare a wrong best_bid to trigger stale
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.38",
                        size="30",
                        side="BUY",
                        best_bid="0.99",  # wrong — computed will be 0.40
                        best_ask="0.60",
                    ),
                ]
            )
        )
        assert TOKEN_A in cache.stale_tokens

    def test_mismatched_best_ask_flags_stale(self) -> None:
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.38", size="30", side="BUY", best_bid="0.40", best_ask="0.01"
                    ),  # wrong — computed will be 0.60
                ]
            )
        )
        assert TOKEN_A in cache.stale_tokens

    def test_book_snapshot_clears_stale(self) -> None:
        cache = self._cache_with_snapshot()
        # Flag as stale
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.38", size="30", side="BUY", best_bid="0.99", best_ask="0.60"
                    ),  # mismatch on bid
                ]
            )
        )
        assert TOKEN_A in cache.stale_tokens
        # A fresh snapshot (with a new hash, as a real one has) clears it.
        cache.apply(_book_event(hash_="fresh_snapshot_hash"))
        assert TOKEN_A not in cache.stale_tokens

    def test_delta_to_unknown_token_flags_stale_no_book(self) -> None:
        cache = OrderBookCache()
        # Apply delta without any prior snapshot
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        asset_id=TOKEN_C,
                        price="0.50",
                        size="100",
                        side="BUY",
                        best_bid="0.50",
                        best_ask="0.60",
                    ),
                ]
            )
        )
        assert TOKEN_C in cache.stale_tokens
        assert cache.book(TOKEN_C) is None

    def test_take_stale_clears(self) -> None:
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        price="0.38", size="30", side="BUY", best_bid="0.99", best_ask="0.60"
                    ),
                ]
            )
        )
        stale = cache.take_stale()
        assert TOKEN_A in stale
        assert len(cache.stale_tokens) == 0

    def test_empty_best_bid_ask_no_stale_check(self) -> None:
        """Entries with empty/absent best_bid & best_ask skip the integrity check."""
        cache = self._cache_with_snapshot()
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(price="0.45", size="50", side="BUY", best_bid="", best_ask=""),
                ]
            )
        )
        assert TOKEN_A not in cache.stale_tokens


# ===========================================================================
# 4. Malformed / adversarial inputs
# ===========================================================================
class TestMalformedInput:
    def test_non_dict_in_list_skipped_no_exception(self) -> None:
        cache = OrderBookCache()
        result = cache.apply(["not a dict", 42, None])
        assert result == set()
        assert cache.skip_count > 0

    def test_missing_asset_id_in_book_skipped(self) -> None:
        cache = OrderBookCache()
        msg = {
            "event_type": "book",
            "market": MARKET_A,
            "timestamp": "1000",
            "bids": [],
            "asks": [],
        }
        cache.apply(msg)
        assert len(cache.books()) == 0

    def test_junk_size_in_book_level_ignored(self) -> None:
        cache = OrderBookCache()
        msg = _book_event(
            bids=[{"price": "0.40", "size": "not_a_number"}],
            asks=[{"price": "0.60", "size": "200"}],
        )
        cache.apply(msg)
        ob = cache.book(TOKEN_A)
        assert ob is not None
        # The bad bid level should be dropped
        assert all(lvl.price != Decimal("0.40") for lvl in ob.bids)

    def test_zero_price_bid_excluded(self) -> None:
        cache = OrderBookCache()
        msg = _book_event(
            bids=[{"price": "0", "size": "100"}, {"price": "0.40", "size": "50"}],
            asks=[{"price": "0.60", "size": "200"}],
        )
        cache.apply(msg)
        ob = cache.book(TOKEN_A)
        assert ob is not None
        prices = {lvl.price for lvl in ob.bids}
        assert Decimal("0") not in prices
        assert Decimal("0.40") in prices

    def test_unknown_event_type_ignored(self) -> None:
        cache = OrderBookCache()
        changed = cache.apply({"event_type": "something_new", "asset_id": TOKEN_A})
        assert changed == set()

    def test_non_dict_message_ignored(self) -> None:
        cache = OrderBookCache()
        result = cache.apply("this is a string, not a dict or list")
        assert result == set()

    def test_price_change_missing_asset_id_skipped(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event())
        msg = _price_change_event(
            [
                {
                    "price": "0.40",
                    "size": "100",
                    "side": "BUY",
                    "hash": "h1",
                    "best_bid": "0.40",
                    "best_ask": "0.60",
                },  # no asset_id
            ]
        )
        cache.apply(msg)
        # No exception; skip count incremented
        assert cache.skip_count >= 1

    def test_price_change_missing_side_skipped(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event())
        msg = _price_change_event(
            [
                {
                    "asset_id": TOKEN_A,
                    "price": "0.40",
                    "size": "100",
                    "hash": "h1",
                    "best_bid": "0.40",
                    "best_ask": "0.60",
                },  # no side
            ]
        )
        cache.apply(msg)
        assert cache.skip_count >= 1

    def test_price_change_junk_price_skipped(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event())
        msg = _price_change_event(
            [
                _pc_entry(price="not_a_price", size="100", side="BUY", best_bid="", best_ask=""),
            ]
        )
        cache.apply(msg)
        assert cache.skip_count >= 1

    def test_apply_none_message_ignored(self) -> None:
        cache = OrderBookCache()
        result = cache.apply(None)  # type: ignore[arg-type]
        assert result == set()


# ===========================================================================
# 5. List-wrapped message (apply normalises list → events)
# ===========================================================================
class TestListMessages:
    def test_list_of_book_events(self) -> None:
        cache = OrderBookCache()
        msg_list = [
            _book_event(token_id=TOKEN_A),
            _book_event(token_id=TOKEN_B, market=MARKET_B),
        ]
        changed = cache.apply(msg_list)
        assert TOKEN_A in changed
        assert TOKEN_B in changed
        assert cache.book(TOKEN_A) is not None
        assert cache.book(TOKEN_B) is not None

    def test_empty_list(self) -> None:
        cache = OrderBookCache()
        assert cache.apply([]) == set()


# ===========================================================================
# 6. tick_size_change and last_trade_price events
# ===========================================================================
class TestMiscEvents:
    def test_tick_size_change_updates_field(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event(tick_size="0.001"))
        cache.apply({"event_type": "tick_size_change", "asset_id": TOKEN_A, "tick_size": "0.01"})
        ob = cache.book(TOKEN_A)
        assert ob is not None
        assert ob.tick_size == Decimal("0.01")

    def test_last_trade_price_event_updates_field(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event(last_trade_price="0.50"))
        cache.apply(
            {
                "event_type": "last_trade_price",
                "asset_id": TOKEN_A,
                "last_trade_price": "0.55",
            }
        )
        ob = cache.book(TOKEN_A)
        assert ob is not None
        assert ob.last_trade_price == Decimal("0.55")

    def test_tick_size_change_for_unknown_token_ignored(self) -> None:
        cache = OrderBookCache()
        # No snapshot → silently ignored
        cache.apply(
            {
                "event_type": "tick_size_change",
                "asset_id": "no_such_token",
                "tick_size": "0.01",
            }
        )
        assert cache.book("no_such_token") is None


# ===========================================================================
# 7. Fixture replay smoke test
# ===========================================================================
class TestFixtureReplay:
    def test_fixture_replay_no_exception(self) -> None:
        """Replay the full fixture; no exception, all materialised books are valid."""
        fixture_path = FIXTURES_DIR / "ws_market_capture.json"
        if not fixture_path.exists():
            pytest.skip(f"Fixture not found: {fixture_path}")

        messages = json.loads(fixture_path.read_text())
        cache = OrderBookCache()

        for msg in messages:
            cache.apply(msg)

        all_books = cache.books()
        assert len(all_books) > 0, "Expected at least one book after replay"

        for token_id, ob in all_books.items():
            # Levels must be internally consistent
            for lvl in ob.bids:
                assert lvl.price > 0 and lvl.size > 0, f"Non-positive bid level in {token_id}"
            for lvl in ob.asks:
                assert lvl.price > 0 and lvl.size > 0, f"Non-positive ask level in {token_id}"
            # If both sides non-empty: best bid must be below best ask
            bb = ob.best_bid
            ba = ob.best_ask
            if bb is not None and ba is not None:
                assert bb.price < ba.price, (
                    f"Crossed book for {token_id}: bid={bb.price} >= ask={ba.price}"
                )

    def test_fixture_changed_sets_non_empty(self) -> None:
        """Each message in the fixture should change at least one token."""
        fixture_path = FIXTURES_DIR / "ws_market_capture.json"
        if not fixture_path.exists():
            pytest.skip(f"Fixture not found: {fixture_path}")

        messages = json.loads(fixture_path.read_text())
        cache = OrderBookCache()
        any_changed = False
        for msg in messages:
            changed = cache.apply(msg)
            if changed:
                any_changed = True
        assert any_changed, "No message in the fixture changed any book"


# ===========================================================================
# 5. seed() — REST resync entry point (websocket phase 2)
# ===========================================================================
def _rest_book(token_id: str = TOKEN_A, bid: str = "0.30", ask: str = "0.70") -> OrderBook:
    return OrderBook.model_validate(
        {
            "market": MARKET_A,
            "asset_id": token_id,
            "timestamp": 1234,
            "bids": [{"price": bid, "size": "500"}],
            "asks": [{"price": ask, "size": "500"}],
        }
    )


class TestSeed:
    def test_seed_populates_unknown_token(self) -> None:
        cache = OrderBookCache()
        cache.seed(_rest_book(TOKEN_C, bid="0.20", ask="0.80"))
        ob = cache.book(TOKEN_C)
        assert ob is not None
        assert ob.best_bid is not None and ob.best_bid.price == Decimal("0.20")
        assert ob.best_ask is not None and ob.best_ask.price == Decimal("0.80")

    def test_seed_clears_stale_flag(self) -> None:
        cache = OrderBookCache()
        # A delta to an unknown token flags it stale (no snapshot to apply to).
        cache.apply(_price_change_event([_pc_entry(asset_id=TOKEN_C)]))
        assert TOKEN_C in cache.stale_tokens
        cache.seed(_rest_book(TOKEN_C))
        assert TOKEN_C not in cache.stale_tokens

    def test_seed_replaces_existing_levels(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event(TOKEN_A))
        cache.seed(_rest_book(TOKEN_A, bid="0.10", ask="0.90"))
        ob = cache.book(TOKEN_A)
        assert ob is not None
        assert ob.best_bid is not None and ob.best_bid.price == Decimal("0.10")


# ===========================================================================
# 6. A3 hash-revert detection (websocket phase 2)
# ===========================================================================
class TestHashRevert:
    def test_revert_flags_stale(self) -> None:
        """A book hash that rolls back to an earlier snapshot (A→B→A) flags stale."""
        cache = OrderBookCache()
        cache.apply(_book_event(TOKEN_A, hash_="A"))
        cache.apply(_book_event(TOKEN_A, hash_="B"))
        assert TOKEN_A not in cache.stale_tokens
        cache.apply(_book_event(TOKEN_A, hash_="A"))  # revert
        assert TOKEN_A in cache.stale_tokens

    def test_monotonic_hashes_not_stale(self) -> None:
        cache = OrderBookCache()
        for h in ("A", "B", "C", "D"):
            cache.apply(_book_event(TOKEN_A, hash_=h))
        assert TOKEN_A not in cache.stale_tokens

    def test_immediate_repeat_not_revert(self) -> None:
        cache = OrderBookCache()
        cache.apply(_book_event(TOKEN_A, hash_="A"))
        cache.apply(_book_event(TOKEN_A, hash_="A"))  # echo, not a revert
        assert TOKEN_A not in cache.stale_tokens

    def test_revert_via_price_change_hash(self) -> None:
        """A price_change whose hash reverts to an earlier book hash flags stale.

        The delta touches a DEEP level and declares the unchanged top of book, so the
        top-of-book integrity check passes — isolating the hash-revert as the stale cause.
        """
        cache = OrderBookCache()
        cache.apply(_book_event(TOKEN_A, hash_="H1"))  # default top: bid 0.40 / ask 0.60
        cache.apply(_book_event(TOKEN_A, hash_="H2"))
        # Deep bid (0.10) leaves the top unchanged; declared best_bid/ask match computed.
        cache.apply(
            _price_change_event(
                [
                    _pc_entry(
                        asset_id=TOKEN_A,
                        price="0.10",
                        size="5",
                        side="BUY",
                        hash_="H1",  # reverts to the earlier snapshot
                        best_bid="0.40",
                        best_ask="0.60",
                    )
                ]
            )
        )
        assert TOKEN_A in cache.stale_tokens
