"""Offline tests for the REST-confirm barrier (websocket phase 3, R1). No network."""

from __future__ import annotations

import asyncio

import httpx

from polyarb.detectors.base import Detector, Snapshot
from polyarb.detectors.complement import ComplementDetector
from polyarb.detectors.negrisk_basket import NegRiskBasketDetector
from polyarb.engine.confirm import ConfirmContext, confirm_candidate
from polyarb.models import DetectorKind, OrderBook
from tests.helpers import make_book, make_event, make_market


class FakeClob:
    """Serves pre-supplied REST books; raises httpx error for tokens in ``fail`` or unknown."""

    def __init__(self, books: dict[str, OrderBook], *, fail: set[str] | None = None) -> None:
        self._books = books
        self._fail = fail or set()
        self.calls: list[str] = []

    async def get_order_book(self, token_id: str) -> OrderBook:
        self.calls.append(token_id)
        if token_id in self._fail or token_id not in self._books:
            raise httpx.HTTPError("boom")
        return self._books[token_id]


_DETECTORS: dict[DetectorKind, Detector] = {
    DetectorKind.COMPLEMENT: ComplementDetector(),
    DetectorKind.NEGRISK_BASKET: NegRiskBasketDetector(),
}


def _complement_candidate(yes_book: OrderBook, no_book: OrderBook):
    market = make_market("0xA", yes="y", no="n")
    snap = Snapshot(books={"y": yes_book, "n": no_book}, markets=[market])
    opps = list(ComplementDetector().detect(snap))
    assert opps, "fixture should produce a complement opp"
    return opps[0], market


def _ctx_for(market) -> ConfirmContext:
    return ConfirmContext(markets_by_condition={market.condition_id: market})


# --- complement -----------------------------------------------------------


def test_confirm_returns_fresh_opp_when_edge_holds() -> None:
    yes = make_book("y", asks=[("0.40", "100")], bids=[("0.30", "100")])
    no = make_book("n", asks=[("0.50", "100")], bids=[("0.45", "100")])
    candidate, market = _complement_candidate(yes, no)
    # Fresh REST books still show the under edge (same shape).
    clob = FakeClob({"y": yes, "n": no})

    confirmed = asyncio.run(
        confirm_candidate(candidate, ctx=_ctx_for(market), clob=clob, detectors=_DETECTORS)  # type: ignore[arg-type]
    )
    assert confirmed is not None
    assert confirmed.detector == DetectorKind.COMPLEMENT
    assert confirmed.condition_ids == candidate.condition_ids
    assert {(leg.token_id, leg.side) for leg in confirmed.legs} == {("y", "buy"), ("n", "buy")}
    assert set(clob.calls) == {"y", "n"}  # re-fetched exactly the candidate's legs


def test_confirm_rejects_when_edge_gone() -> None:
    yes = make_book("y", asks=[("0.40", "100")], bids=[("0.30", "100")])
    no = make_book("n", asks=[("0.50", "100")], bids=[("0.45", "100")])
    candidate, market = _complement_candidate(yes, no)
    # Fresh books: the under edge has evaporated (asks now sum > 1, no over either).
    fresh_yes = make_book("y", asks=[("0.60", "100")], bids=[("0.30", "100")])
    fresh_no = make_book("n", asks=[("0.55", "100")], bids=[("0.20", "100")])
    clob = FakeClob({"y": fresh_yes, "n": fresh_no})

    assert (
        asyncio.run(
            confirm_candidate(candidate, ctx=_ctx_for(market), clob=clob, detectors=_DETECTORS)  # type: ignore[arg-type]
        )
        is None
    )


def test_confirm_rejects_on_structure_change_under_to_over() -> None:
    yes = make_book("y", asks=[("0.40", "100")], bids=[("0.30", "100")])
    no = make_book("n", asks=[("0.50", "100")], bids=[("0.45", "100")])
    candidate, market = _complement_candidate(yes, no)  # an UNDER (buy/buy) candidate
    # Fresh books now present an OVER (sell/sell): bids sum > 1, asks high (no under).
    fresh_yes = make_book("y", asks=[("0.90", "100")], bids=[("0.60", "100")])
    fresh_no = make_book("n", asks=[("0.90", "100")], bids=[("0.55", "100")])
    clob = FakeClob({"y": fresh_yes, "n": fresh_no})

    # The over opp has a different leg signature → the under candidate is NOT confirmed.
    assert (
        asyncio.run(
            confirm_candidate(candidate, ctx=_ctx_for(market), clob=clob, detectors=_DETECTORS)  # type: ignore[arg-type]
        )
        is None
    )


def test_confirm_rejects_when_market_context_missing() -> None:
    yes = make_book("y", asks=[("0.40", "100")], bids=[("0.30", "100")])
    no = make_book("n", asks=[("0.50", "100")], bids=[("0.45", "100")])
    candidate, _ = _complement_candidate(yes, no)
    clob = FakeClob({"y": yes, "n": no})
    # Empty context → no market to rebuild the snapshot → not confirmed.
    assert (
        asyncio.run(
            confirm_candidate(candidate, ctx=ConfirmContext(), clob=clob, detectors=_DETECTORS)  # type: ignore[arg-type]
        )
        is None
    )


def test_confirm_rejects_when_all_book_fetches_fail() -> None:
    yes = make_book("y", asks=[("0.40", "100")], bids=[("0.30", "100")])
    no = make_book("n", asks=[("0.50", "100")], bids=[("0.45", "100")])
    candidate, market = _complement_candidate(yes, no)
    clob = FakeClob({}, fail={"y", "n"})  # every fetch raises → empty books → no opp
    assert (
        asyncio.run(
            confirm_candidate(candidate, ctx=_ctx_for(market), clob=clob, detectors=_DETECTORS)  # type: ignore[arg-type]
        )
        is None
    )


# --- negrisk basket (event-scoped) ---------------------------------------


def test_confirm_basket_via_event_context() -> None:
    # 3 mutually-exclusive YES legs summing < 1 → a basket under.
    markets = [
        make_market("0xc1", yes="y1", no="n1", group_item_title="A"),
        make_market("0xc2", yes="y2", no="n2", group_item_title="B"),
        make_market("0xc3", yes="y3", no="n3", group_item_title="C"),
    ]
    event = make_event(markets)
    books = {
        "y1": make_book("y1", asks=[("0.30", "100")], bids=[("0.25", "100")]),
        "y2": make_book("y2", asks=[("0.30", "100")], bids=[("0.25", "100")]),
        "y3": make_book("y3", asks=[("0.30", "100")], bids=[("0.25", "100")]),
    }
    snap = Snapshot(books=books, event=event)
    candidates = list(NegRiskBasketDetector().detect(snap))
    assert candidates, "fixture should produce a basket opp"
    candidate = candidates[0]

    ctx = ConfirmContext(events_by_id={event.id: event})
    clob = FakeClob(books)
    confirmed = asyncio.run(
        confirm_candidate(candidate, ctx=ctx, clob=clob, detectors=_DETECTORS)  # type: ignore[arg-type]
    )
    assert confirmed is not None
    assert confirmed.detector == DetectorKind.NEGRISK_BASKET
    assert set(clob.calls) == {"y1", "y2", "y3"}
