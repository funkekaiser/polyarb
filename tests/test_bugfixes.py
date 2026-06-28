"""Regression tests for bugs found in the adversarial bug-hunt (see docs/TESTING.md)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polyarb.detectors.base import ZERO, Profit, Snapshot, make_opportunity
from polyarb.detectors.dependency import DependencyDetector
from polyarb.engine.scanner import _days_to_resolution
from polyarb.models import DetectorKind, Market, OrderBook
from polyarb.pricing.fees import taker_fee
from polyarb.resolution.relations import (
    Comparator,
    ComparatorKind,
    MarketTags,
    Relation,
    add_relation,
    generate_ladder_relations,
)
from polyarb.sinks.notify import NullNotifier, build_notifier
from tests.helpers import make_book, make_event, make_market

ONE = Decimal(1)


# Bug 1 — naive end_date must not raise (would poison a whole scan pass).
def test_naive_end_date_does_not_crash_days_helper() -> None:
    m = Market.model_validate(
        {
            "id": "1",
            "conditionId": "0xC",
            "question": "q",
            "clobTokenIds": '["Y","N"]',
            "endDate": "2099-01-01T00:00:00",
        }  # no Z → naive datetime
    )
    assert m.end_date is not None and m.end_date.tzinfo is None
    days = _days_to_resolution([m], datetime.now(UTC))
    assert days["0xC"] > 0


# Bug 2 — taker_fee must never be negative for out-of-range prices.
def test_taker_fee_never_negative() -> None:
    assert taker_fee(Decimal("1.1"), ONE, Decimal("0.07")) == 0
    assert taker_fee(Decimal("-0.1"), ONE, Decimal("0.07")) == 0
    assert taker_fee(Decimal("0.5"), ONE, Decimal("0.07")) > 0  # valid range still charged


# Bug 3 — a today-resolving (days=0) arb gets a real annualized, not None.
def _profit() -> Profit:
    return Profit(cost=Decimal("0.90"), gross_profit=Decimal("0.10"), fees=ZERO, gas=ZERO)


def test_days_zero_annualizes_high_not_none() -> None:
    opp0 = make_opportunity(
        detector=DetectorKind.NEGRISK_BASKET,
        description="d",
        condition_ids=["0x1"],
        legs=[],
        profit=_profit(),
        executable_size=ONE,
        realizes="resolution",
        days_to_resolution=0,
    )
    assert opp0.annualized is not None
    # days floored to 1 → (0.10/0.90) * 365
    assert opp0.annualized == (Decimal("0.10") / Decimal("0.90")) * Decimal(365)


def test_days_none_leaves_annualized_none() -> None:
    opp = make_opportunity(
        detector=DetectorKind.NEGRISK_BASKET,
        description="d",
        condition_ids=["0x1"],
        legs=[],
        profit=_profit(),
        executable_size=ONE,
        realizes="resolution",
        days_to_resolution=None,
    )
    assert opp.annualized is None


# Bug 4 — OrderBook coerces a fractional float timestamp.
def test_order_book_coerces_float_timestamp() -> None:
    book = OrderBook(market="0xc", asset_id="t", timestamp_ms=1234567890.9, bids=[], asks=[])
    assert book.timestamp_ms == 1234567890


# Bug 5 — non-binary markets must not IndexError.
def test_yes_outcome_guards_non_binary() -> None:
    m = Market.model_validate(
        {"id": "1", "conditionId": "0x1", "question": "q", "clobTokenIds": "[]"}
    )
    assert not m.is_binary
    with pytest.raises(ValueError):
        m.yes_outcome()


def test_event_outcomes_skips_non_binary() -> None:
    non_binary = Market.model_validate(
        {"id": "1", "conditionId": "0xNB", "question": "q", "clobTokenIds": "[]"}
    )
    binary = make_market("0xB", yes="y", no="n")
    event = make_event([non_binary, binary])
    outs = event.outcomes()
    assert [o.condition_id for o in outs] == ["0xB"]


# Bug 6 — notifier lifecycle: aclose exists and is safe.
def test_notifier_aclose() -> None:
    async def run() -> None:
        await NullNotifier().aclose()
        webhook = build_notifier("webhook", "https://example.com/hook")
        await webhook.aclose()  # must not raise; closes the owned client

    asyncio.run(run())


# Bug 7 — a self-loop relation must not emit (and add_relation rejects it).
def test_dependency_skips_self_loop() -> None:
    m = make_market("c1", yes="y", no="n")
    snap = Snapshot(
        markets=[m],
        relations=[Relation("c1", "c1", "self")],
        books={
            "y": make_book("y", asks=[("0.40", "10")]),
            "n": make_book("n", asks=[("0.40", "10")]),
        },
    )
    assert list(DependencyDetector().detect(snap)) == []


def test_add_relation_rejects_self_loop() -> None:
    with pytest.raises(ValueError):
        add_relation("x", "x", "loop")


# Bug 8 — ladder must not emit a relation between two equal-bound markets.
def _tag(cid: str, bound: str) -> MarketTags:
    return MarketTags(
        cid, "ETH", Comparator.THRESHOLD_GTE, bound, ComparatorKind.CUMULATIVE_TOUCH, "fp"
    )


def test_ladder_skips_equal_bounds() -> None:
    rels = generate_ladder_relations([_tag("a", "8000"), _tag("b", "8000"), _tag("c", "10000")])
    pairs = {(r.antecedent_condition_id, r.consequent_condition_id) for r in rels}
    assert ("a", "b") not in pairs and ("b", "a") not in pairs
    # only the 10000 ⇒ 8000 rung (higher ⇒ lower) survives
    assert len(rels) == 1
