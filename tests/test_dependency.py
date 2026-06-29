"""Dependency detector — A ⇒ B violations and the locked YES_B + NO_A trade."""

from __future__ import annotations

from decimal import Decimal

from polyarb.detectors.base import Snapshot
from polyarb.detectors.dependency import DependencyDetector, dependency_profit
from polyarb.pricing.fees import taker_fee
from polyarb.resolution.relations import Relation
from tests.helpers import make_book, make_market

ZERO = Decimal(0)
ONE = Decimal(1)


def test_dependency_profit_formula() -> None:
    # cost = a_yes_B + a_no_A; min payoff 1.
    p = dependency_profit(Decimal("0.30"), Decimal("0.30"), ZERO, ZERO)
    assert p.cost == Decimal("0.60")
    assert p.gross_profit == Decimal("0.40")


def _snapshot(a_no_ask: str, b_yes_ask: str) -> Snapshot:
    market_a = make_market("0xA", yes="yA", no="nA")
    market_b = make_market("0xB", yes="yB", no="nB")
    relation = Relation("0xA", "0xB", "A ⇒ B")
    return Snapshot(
        markets=[market_a, market_b],
        relations=[relation],
        books={
            "nA": make_book("nA", asks=[(a_no_ask, "50")]),
            "yB": make_book("yB", asks=[(b_yes_ask, "80")]),
        },
    )


def test_detector_emits_on_violation() -> None:
    snap = _snapshot(a_no_ask="0.30", b_yes_ask="0.30")  # cost 0.60 < 1
    opps = list(DependencyDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.gross_profit == Decimal("0.40")
    assert opp.executable_size == Decimal(50)  # thinnest leg
    assert opp.realizes == "resolution"
    assert opp.condition_ids == ["0xA", "0xB"]


def test_detector_silent_when_no_violation() -> None:
    snap = _snapshot(a_no_ask="0.60", b_yes_ask="0.60")  # cost 1.20 ≥ 1
    assert list(DependencyDetector().detect(snap)) == []


def test_detector_skips_unknown_markets() -> None:
    snap = _snapshot(a_no_ask="0.30", b_yes_ask="0.30")
    snap.relations = [Relation("0xMISSING", "0xB", "dangling")]
    assert list(DependencyDetector().detect(snap)) == []


# ---------------------------------------------------------------------------
# DEPTH — depth-walk on YES_B + NO_A captures multiple profitable levels
# ---------------------------------------------------------------------------


def _snapshot_multilevel(
    a_no_levels: list[tuple[str, str]],
    b_yes_levels: list[tuple[str, str]],
) -> Snapshot:
    market_a = make_market("0xA", yes="yA", no="nA")
    market_b = make_market("0xB", yes="yB", no="nB")
    relation = Relation("0xA", "0xB", "A ⇒ B")
    return Snapshot(
        markets=[market_a, market_b],
        relations=[relation],
        books={
            "nA": make_book("nA", asks=a_no_levels),
            "yB": make_book("yB", asks=b_yes_levels),
        },
    )


def test_depth_walk_captures_multiple_levels() -> None:
    """Depth-walk on YES_B and NO_A includes both profitable ask levels.

    YES_B: [(0.30, 50), (0.35, 80)], NO_A: [(0.30, 50), (0.35, 80)].
      Level 1: 0.30+0.30=0.60 < 1 → profitable; chunk=50.
      Level 2: 0.35+0.35=0.70 < 1 → profitable; chunk=80.
      Total size = 130.  Best-ask-only would give min(50,50)=50.
    """
    snap = _snapshot_multilevel(
        a_no_levels=[("0.30", "50"), ("0.35", "80")],
        b_yes_levels=[("0.30", "50"), ("0.35", "80")],
    )
    opps = list(DependencyDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.executable_size > Decimal(50)  # beyond best-ask-level depth
    assert opp.executable_size == Decimal(130)  # both profitable levels


# ---------------------------------------------------------------------------
# CROSSED — crossed leg books cause the relation to be skipped
# ---------------------------------------------------------------------------


def test_crossed_no_a_book_skips_relation() -> None:
    """A crossed NO_A book (bid >= ask) causes the relation to be skipped."""
    market_a = make_market("0xA", yes="yA", no="nA")
    market_b = make_market("0xB", yes="yB", no="nB")
    relation = Relation("0xA", "0xB", "A ⇒ B")
    snap = Snapshot(
        markets=[market_a, market_b],
        relations=[relation],
        books={
            "nA": make_book("nA", bids=[("0.60", "100")], asks=[("0.20", "100")]),  # crossed
            "yB": make_book("yB", asks=[("0.30", "80")]),
        },
    )
    assert list(DependencyDetector().detect(snap)) == []


def test_crossed_yes_b_book_skips_relation() -> None:
    """A crossed YES_B book (bid >= ask) causes the relation to be skipped."""
    market_a = make_market("0xA", yes="yA", no="nA")
    market_b = make_market("0xB", yes="yB", no="nB")
    relation = Relation("0xA", "0xB", "A ⇒ B")
    snap = Snapshot(
        markets=[market_a, market_b],
        relations=[relation],
        books={
            "nA": make_book("nA", asks=[("0.30", "50")]),
            "yB": make_book("yB", bids=[("0.50", "100")], asks=[("0.20", "100")]),  # crossed
        },
    )
    assert list(DependencyDetector().detect(snap)) == []


# ---------------------------------------------------------------------------
# GAS — gas guard suppresses opportunities whose total net profit ≤ gas
# ---------------------------------------------------------------------------


def test_dependency_gas_guard() -> None:
    """gas >= size * net_profit → no emission; gas just below threshold → emits.

    a_no_ask=0.30 (50 shares), b_yes_ask=0.30 (80 shares).
    size = min(50, 80) = 50; net_profit = 1 - 0.60 = 0.40/set.
    total_net = 50 * 0.40 = 20.00.
    gas=20.01 → 20.00 - 20.01 = -0.01 ≤ 0 → suppress.
    gas=19.99 → 20.00 - 19.99 =  0.01 > 0 → emit.
    """
    market_a = make_market("0xA", yes="yA", no="nA")
    market_b = make_market("0xB", yes="yB", no="nB")
    relation = Relation("0xA", "0xB", "A ⇒ B")
    books = {
        "nA": make_book("nA", asks=[("0.30", "50")]),
        "yB": make_book("yB", asks=[("0.30", "80")]),
    }
    markets = [market_a, market_b]
    relations = [relation]

    snap_high = Snapshot(markets=markets, relations=relations, books=books, gas=Decimal("20.01"))
    assert list(DependencyDetector().detect(snap_high)) == []

    snap_low = Snapshot(markets=markets, relations=relations, books=books, gas=Decimal("19.99"))
    opps = list(DependencyDetector().detect(snap_low))
    assert len(opps) == 1


# ---------------------------------------------------------------------------
# PER-LEG FEES — YES_B and NO_A are each charged their own market's rate
# ---------------------------------------------------------------------------


def test_per_leg_fee_rates_bind_to_correct_leg() -> None:
    """B's rate is charged on YES_B, A's on NO_A — pinned via distinct prices AND rates.

    A `[fee_b, fee_a]`→`[fee_a, fee_b]` swap in the detector would change the fee total here
    (distinct prices make it permutation-sensitive), so this catches the wiring, not just the
    walk. Both rates are non-zero so a swap is observable (the default helpers are fee-free).
    """
    market_a = make_market("0xA", yes="yA", no="nA", fee_rate=0.02)
    market_b = make_market("0xB", yes="yB", no="nB", fee_rate=0.05)
    relation = Relation("0xA", "0xB", "A ⇒ B")
    snap = Snapshot(
        markets=[market_a, market_b],
        relations=[relation],
        books={
            "nA": make_book("nA", asks=[("0.40", "100")]),  # NO_A leg → market A's rate (0.02)
            "yB": make_book("yB", asks=[("0.30", "100")]),  # YES_B leg → market B's rate (0.05)
        },
    )
    opps = list(DependencyDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]

    expected_fees = taker_fee(Decimal("0.30"), ONE, Decimal("0.05")) + taker_fee(
        Decimal("0.40"), ONE, Decimal("0.02")
    )
    swapped_fees = taker_fee(Decimal("0.30"), ONE, Decimal("0.02")) + taker_fee(
        Decimal("0.40"), ONE, Decimal("0.05")
    )
    assert opp.fees == expected_fees
    assert opp.fees != swapped_fees
