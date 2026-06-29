"""NegRisk basket detector — under-priced basket, convert-is-not-arb, annualized."""

from __future__ import annotations

from decimal import Decimal

from polyarb.detectors.base import Snapshot
from polyarb.detectors.negrisk_basket import (
    NegRiskBasketDetector,
    basket_profit,
    negrisk_convert_pnl,
)
from polyarb.pricing.fees import taker_fee
from tests.helpers import make_book, make_event, make_market

ONE = Decimal(1)
ZERO = Decimal(0)


def _three_outcome_snapshot(
    ask_prices: list[str], *, days: dict[str, int] | None = None
) -> Snapshot:
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(len(ask_prices))
    ]
    event = make_event(markets, neg_risk=True)
    books = {
        f"y{i}": make_book(f"y{i}", asks=[(price, "100")]) for i, price in enumerate(ask_prices)
    }
    return Snapshot(event=event, books=books, days_to_resolution=days or {})


def test_basket_profit_formula() -> None:
    p = basket_profit([Decimal("0.30"), Decimal("0.30"), Decimal("0.30")], [ZERO, ZERO, ZERO])
    assert p.cost == Decimal("0.90")
    assert p.gross_profit == Decimal("0.10")
    assert p.net_profit == Decimal("0.10")


def test_convert_pnl_is_always_zero() -> None:
    # Convert is capital-efficiency, never profit — regardless of prices.
    assert negrisk_convert_pnl([Decimal("0.1"), Decimal("0.2"), Decimal("0.3")]) == ZERO
    assert negrisk_convert_pnl([Decimal("0.9")]) == ZERO


def test_detector_emits_when_basket_underpriced() -> None:
    snap = _three_outcome_snapshot(["0.30", "0.30", "0.30"])
    opps = list(NegRiskBasketDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.net_profit == Decimal("0.10")
    assert opp.realizes == "resolution"
    assert len(opp.legs) == 3
    assert opp.executable_size == Decimal(100)


def test_detector_no_arb_when_sum_ge_one() -> None:
    snap = _three_outcome_snapshot(["0.40", "0.40", "0.40"])  # Σ = 1.20
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_detector_skips_when_a_leg_book_missing() -> None:
    snap = _three_outcome_snapshot(["0.30", "0.30", "0.30"])
    del snap.books["y2"]  # one outcome unfillable → cannot lock the basket
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_annualized_computed_from_days() -> None:
    snap = _three_outcome_snapshot(["0.30", "0.30", "0.30"], days={"0x0": 365})
    opp = next(iter(NegRiskBasketDetector().detect(snap)))
    assert opp.days_to_resolution == 365
    assert opp.annualized is not None
    # net 0.10 on cost 0.90 over 365 days ≈ 0.1111 annualized
    assert opp.annualized == (Decimal("0.10") / Decimal("0.90")) * (Decimal(365) / Decimal(365))


def test_not_multi_outcome_event_ignored() -> None:
    # Only 2 markets → not a NegRisk basket (need N>=3).
    markets = [make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True) for i in range(2)]
    snap = Snapshot(
        event=make_event(markets, neg_risk=True),
        books={
            "y0": make_book("y0", asks=[("0.30", "100")]),
            "y1": make_book("y1", asks=[("0.30", "100")]),
        },
    )
    assert list(NegRiskBasketDetector().detect(snap)) == []


# ---------------------------------------------------------------------------
# DEPTH — multi-level depth-walk captures more than the best ask level
# ---------------------------------------------------------------------------


def test_depth_walk_captures_beyond_best_ask_level() -> None:
    """Depth-walk includes both ask levels; best-ask-only sizing would give 50, not 110.

    Each outcome has asks [(0.25, 50), (0.28, 60)]:
      Level 1: Σ=3*0.25=0.75 < 1 → profitable; chunk=50 (min across all three legs).
      Level 2: Σ=3*0.28=0.84 < 1 → profitable; chunk=60 (all three legs at level 2).
      Total size = 110.  Old best-ask-only would give 50 (depth at 0.25 only).
    """
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(3)
    ]
    event = make_event(markets, neg_risk=True)
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.25", "50"), ("0.28", "60")]) for i in range(3)}
    snap = Snapshot(event=event, books=books)
    opps = list(NegRiskBasketDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.executable_size > Decimal(50)  # beyond the single best-ask-level depth
    assert opp.executable_size == Decimal(110)  # all profitable levels included


# ---------------------------------------------------------------------------
# CROSSED — event skipped when any outcome's YES book is crossed
# ---------------------------------------------------------------------------


def test_crossed_yes_book_skips_entire_event() -> None:
    """A crossed YES book on any outcome causes the whole basket to be skipped.

    y0 is crossed (bid 0.60 >= ask 0.20); y1 and y2 are normal.
    Even though the basket sum looks low, we must not emit.
    """
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(3)
    ]
    event = make_event(markets, neg_risk=True)
    books = {
        "y0": make_book("y0", bids=[("0.60", "100")], asks=[("0.20", "100")]),  # crossed
        "y1": make_book("y1", asks=[("0.30", "100")]),
        "y2": make_book("y2", asks=[("0.30", "100")]),
    }
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


# ---------------------------------------------------------------------------
# GAS — gas guard suppresses opportunities whose total net profit ≤ gas
# ---------------------------------------------------------------------------


def test_gas_guard_suppresses_negrisk_opp() -> None:
    """gas >= size * net_profit → no emission; gas just below threshold → emits.

    Three outcomes at 0.30 each: net_profit=0.10/set, size=100 → total_net=10.00.
    gas=10.01 → 10.00 - 10.01 = -0.01 ≤ 0 → suppress.
    gas=9.99  → 10.00 - 9.99  =  0.01 > 0 → emit.
    """
    snap_base = _three_outcome_snapshot(["0.30", "0.30", "0.30"])

    snap_high = Snapshot(event=snap_base.event, books=snap_base.books, gas=Decimal("10.01"))
    assert list(NegRiskBasketDetector().detect(snap_high)) == []

    # Exact boundary: total_net == gas → 0, guard is `<= 0`, so it must suppress (no off-by-one).
    snap_exact = Snapshot(event=snap_base.event, books=snap_base.books, gas=Decimal("10.00"))
    assert list(NegRiskBasketDetector().detect(snap_exact)) == []

    snap_low = Snapshot(event=snap_base.event, books=snap_base.books, gas=Decimal("9.99"))
    opps = list(NegRiskBasketDetector().detect(snap_low))
    assert len(opps) == 1


# ---------------------------------------------------------------------------
# PER-LEG FEES — each outcome's fee rate is applied to its own YES leg only
# ---------------------------------------------------------------------------


def test_per_leg_fee_rates_applied_independently() -> None:
    """Each outcome's fee rate is charged on ITS OWN leg — pinned via distinct prices.

    Market 0: fee_rate=0.05 @0.25, Market 1: fee_rate=0.02 @0.30, Market 2: fee-free @0.35.
    Because both the prices AND the rates differ per leg, the fee total is not
    permutation-invariant: a scrambled rate→leg mapping would yield a different total. This
    pins the alignment that `test_per_leg_fees_bind_to_their_own_leg` proves for the walk,
    now through the detector's market→leg wiring.
    """
    markets = [
        make_market("0x0", yes="y0", no="n0", neg_risk=True, fee_rate=0.05, group_item_title="O0"),
        make_market("0x1", yes="y1", no="n1", neg_risk=True, fee_rate=0.02, group_item_title="O1"),
        make_market("0x2", yes="y2", no="n2", neg_risk=True, group_item_title="O2"),  # fee-free
    ]
    event = make_event(markets, neg_risk=True)
    books = {
        "y0": make_book("y0", asks=[("0.25", "100")]),
        "y1": make_book("y1", asks=[("0.30", "100")]),
        "y2": make_book("y2", asks=[("0.35", "100")]),
    }
    snap = Snapshot(event=event, books=books)
    opps = list(NegRiskBasketDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]

    # Correct alignment: rate 0.05 on the 0.25 leg, 0.02 on 0.30, fee-free on 0.35.
    expected_fees = (
        taker_fee(Decimal("0.25"), ONE, Decimal("0.05"))
        + taker_fee(Decimal("0.30"), ONE, Decimal("0.02"))
        + taker_fee(Decimal("0.35"), ONE, ZERO)
    )
    # A scrambled mapping (e.g. 0.05 onto the 0.35 leg) would total differently.
    scrambled_fees = (
        taker_fee(Decimal("0.35"), ONE, Decimal("0.05"))
        + taker_fee(Decimal("0.30"), ONE, Decimal("0.02"))
        + taker_fee(Decimal("0.25"), ONE, ZERO)
    )
    assert opp.fees == expected_fees
    assert opp.fees != scrambled_fees
