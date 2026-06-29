"""NegRisk basket detector — under-priced basket, convert-is-not-arb, annualized."""

from __future__ import annotations

from decimal import Decimal

from polyarb.detectors.base import Snapshot
from polyarb.detectors.negrisk_basket import (
    NegRiskBasketDetector,
    NegRiskDualDetector,
    basket_profit,
    dual_profit,
    live_partition,
    negrisk_convert_pnl,
)
from polyarb.models import DetectorKind, Event, Market
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


def test_basket_horizon_is_max_of_legs() -> None:
    # D3: capital locks until the LATEST leg resolves → annualize on max(leg days), not the
    # first known. Legs {10, 100, 50} → 100. (Old "first known" would have given 10.)
    snap = _three_outcome_snapshot(
        ["0.30", "0.30", "0.30"], days={"0x0": 10, "0x1": 100, "0x2": 50}
    )
    opp = next(iter(NegRiskBasketDetector().detect(snap)))
    assert opp.days_to_resolution == 100


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


# ---------------------------------------------------------------------------
# A1 HARDENING — Exhaustiveness enforcement (augmented / closed / holes)
# ---------------------------------------------------------------------------


def test_augmented_event_skips_basket() -> None:
    """A1(A): augmented negRisk event emits nothing even when Σ YES is clearly < 1.

    An augmented event's partition is not safely exhaustive — outcomes can be added after the
    basket is locked and the 'Other' leg's meaning shifts, so Σ<1 may reflect a correct price,
    not an arb.
    """
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(3)
    ]
    event = Event(
        id="9",
        title="Augmented evt",
        neg_risk=True,
        enable_neg_risk=True,
        neg_risk_augmented=True,
        markets=markets,
    )
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.30", "100")]) for i in range(3)}
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_closed_constituent_dropped_live_remainder_emits() -> None:
    """A1(B): a resolved-NO (closed) market is dropped; the 3 live legs still emit.

    A 4-outcome negRisk event where outcome 0 was eliminated (resolved NO, market closed).
    The remaining 3 live outcomes are still an exhaustive set — exactly one of them wins —
    so the detector SHOULD emit a basket over just those 3 legs.  The closed market's
    condition_id and token_id must NOT appear among the emitted legs.
    """
    closed_market = Market(
        id="10",
        condition_id="0xClosed",
        question="Eliminated outcome?",
        outcomes=["Yes", "No"],
        outcome_prices=["0", "1"],  # resolved NO (YES=0) → safe to drop from the partition
        clob_token_ids=["yClosed", "nClosed"],
        neg_risk=True,
        closed=True,
    )
    live_markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(1, 4)
    ]
    event = Event(
        id="9",
        title="4-outcome event with one eliminated",
        neg_risk=True,
        enable_neg_risk=True,
        markets=[closed_market, *live_markets],
    )
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.30", "100")]) for i in range(1, 4)}
    snap = Snapshot(event=event, books=books)

    opps = list(NegRiskBasketDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    # Exactly the 3 live legs, not the closed one.
    assert len(opp.legs) == 3
    assert "0xClosed" not in opp.condition_ids
    assert "yClosed" not in {leg.token_id for leg in opp.legs}
    for i in range(1, 4):
        assert f"0x{i}" in opp.condition_ids


def test_closed_winner_not_dropped_skips_event() -> None:
    """A1(C1-bug): a closed leg that WON (YES≈1) must NOT be dropped — skip the whole event.

    The winner closes first; its losing peers' books go stale-cheap. If we blindly dropped the
    closed leg we'd build a basket over guaranteed losers (Σ tiny → huge fake edge, top rank)
    that pays $0. The resolved YES price (~1) proves it won, so the event must be skipped.
    """
    winner = Market(
        id="10",
        condition_id="0xWinner",
        question="Did the winner win?",
        outcomes=["Yes", "No"],
        outcome_prices=["1", "0"],  # resolved YES — this outcome WON, event is decided
        clob_token_ids=["yWin", "nWin"],
        neg_risk=True,
        closed=True,
    )
    # Losers still live with stale-cheap asks (Σ = 0.15 ⇒ a tempting but fake "85% edge").
    losers = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(1, 4)
    ]
    event = Event(
        id="9",
        title="Decided event (winner closed, losers stale)",
        neg_risk=True,
        enable_neg_risk=True,
        markets=[winner, *losers],
    )
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.05", "100")]) for i in range(1, 4)}
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_closed_unknown_resolution_skips_event() -> None:
    """A1(C1-bug): a closed leg with no resolved price can't be proven a loss → skip the event.

    Without `outcome_prices` we can't tell a resolved-NO (drop-safe) leg from the winner, so the
    conservative choice is to skip rather than risk dropping the winner.
    """
    closed_unknown = Market(
        id="10",
        condition_id="0xUnknown",
        question="Closed, resolution unknown?",
        outcomes=["Yes", "No"],
        clob_token_ids=["yUnk", "nUnk"],  # no outcome_prices
        neg_risk=True,
        closed=True,
    )
    live = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(1, 4)
    ]
    event = Event(
        id="9",
        title="Closed leg, unknown resolution",
        neg_risk=True,
        enable_neg_risk=True,
        markets=[closed_unknown, *live],
    )
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.30", "100")]) for i in range(1, 4)}
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_live_leg_not_accepting_orders_skips_event() -> None:
    """A1(C1): accepting_orders=False on a live outcome is a partition hole → event skipped.

    A live market that isn't accepting orders can't be traded, so the basket can't be locked.
    The detector must skip the WHOLE event — not just that leg — because an uncovered outcome
    means a win there pays the basket $0.
    """
    hole = Market(
        id="1",
        condition_id="0xHole",
        question="Not accepting?",
        outcomes=["Yes", "No"],
        clob_token_ids=["yHole", "nHole"],
        neg_risk=True,
        accepting_orders=False,
    )
    good_markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(2)
    ]
    event = Event(
        id="9",
        title="Hole event — not accepting orders",
        neg_risk=True,
        enable_neg_risk=True,
        markets=[hole, *good_markets],
    )
    # Good legs are profitable (Σ of the good two = 0.60 < 1), but the hole prevents emission.
    # The hole's book is PRESENT so the accepting_orders guard (not the missing-book guard) is
    # the discriminating barrier — remove that guard and this would emit a 3-leg basket.
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.30", "100")]) for i in range(2)}
    books["yHole"] = make_book("yHole", asks=[("0.30", "100")])
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_live_leg_inactive_skips_event() -> None:
    """A1(C2): active=False on a live outcome is a partition hole → event skipped.

    An inactive market is not tradeable; the basket cannot be locked exhaustively.
    """
    hole = Market(
        id="1",
        condition_id="0xHole",
        question="Inactive?",
        outcomes=["Yes", "No"],
        clob_token_ids=["yHole", "nHole"],
        neg_risk=True,
        active=False,
    )
    good_markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(2)
    ]
    event = Event(
        id="9",
        title="Hole event — inactive market",
        neg_risk=True,
        enable_neg_risk=True,
        markets=[hole, *good_markets],
    )
    # Book present so the `active` guard is the discriminating barrier (not the missing book).
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.30", "100")]) for i in range(2)}
    books["yHole"] = make_book("yHole", asks=[("0.30", "100")])
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_live_leg_missing_book_skips_event_exhaustiveness() -> None:
    """A1(C3): YES book absent for a live leg → partition hole → WHOLE event skipped.

    4-outcome event; y0..y2 each have profitable books (Σ=0.60 < 1 for the 3 of them).
    y3's book is entirely absent from snap.books. Even though the other 3 legs look good,
    a win by y3 would pay the basket $0, so the detector must emit nothing.
    """
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(4)
    ]
    event = Event(
        id="9",
        title="4-outcome, 1 book missing",
        neg_risk=True,
        enable_neg_risk=True,
        markets=markets,
    )
    # y3 intentionally omitted — live outcome with no book is a hole.
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.20", "100")]) for i in range(3)}
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_live_leg_crossed_book_skips_event_exhaustiveness() -> None:
    """A1(C4): crossed YES book on any live leg → partition hole → WHOLE event skipped.

    4-outcome event; y0..y2 are normally priced (Σ=0.60 < 1 among the three).
    y3's book is crossed (best_bid=0.70 >= best_ask=0.30): stale/erroneous data means we
    can't safely lock that leg, so the whole event must be skipped.
    """
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(4)
    ]
    event = Event(
        id="9",
        title="4-outcome, 1 crossed book",
        neg_risk=True,
        enable_neg_risk=True,
        markets=markets,
    )
    books = {
        "y0": make_book("y0", asks=[("0.20", "100")]),
        "y1": make_book("y1", asks=[("0.20", "100")]),
        "y2": make_book("y2", asks=[("0.20", "100")]),
        # y3 is crossed: best bid (0.70) >= best ask (0.30) — invalid/stale.
        "y3": make_book("y3", bids=[("0.70", "100")], asks=[("0.30", "100")]),
    }
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_single_live_leg_after_eliminations_emits_nothing() -> None:
    """A1(D): only 1 live leg remains after 2 outcomes are eliminated → no basket.

    A single surviving outcome is a near-certain winner; emitting a 'basket' of 1 leg
    would be a directional bet masquerading as a structural arb.
    """
    closed_markets = [
        Market(
            id=str(i),
            condition_id=f"0xC{i}",
            question=f"Closed{i}?",
            outcomes=["Yes", "No"],
            outcome_prices=["0", "1"],  # resolved NO → dropped, leaving exactly 1 live leg
            clob_token_ids=[f"yC{i}", f"nC{i}"],
            neg_risk=True,
            closed=True,
        )
        for i in range(2)
    ]
    live_market = make_market("0xL", yes="yL", no="nL", neg_risk=True, group_item_title="Last")
    event = Event(
        id="9",
        title="Almost-decided event",
        neg_risk=True,
        enable_neg_risk=True,
        markets=[*closed_markets, live_market],
    )
    books = {"yL": make_book("yL", asks=[("0.30", "100")])}
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


def test_live_partition_skip_augmented_seam() -> None:
    """live_partition skips augmented by default but keeps it when skip_augmented=False.

    Locks the seam the YES basket / NO-dual split relies on: the YES basket needs full
    exhaustiveness (skip augmented), but the NO-dual needs only mutual exclusivity, so it will
    call live_partition(skip_augmented=False). Both should return the 3 live legs there.
    """
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(3)
    ]
    augmented = Event(
        id="9",
        title="Augmented event",
        neg_risk=True,
        enable_neg_risk=True,
        neg_risk_augmented=True,
        markets=markets,
    )
    assert live_partition(augmented) is None  # default skip_augmented=True
    live = live_partition(augmented, skip_augmented=False)
    assert live is not None
    assert [m.condition_id for m in live] == ["0x0", "0x1", "0x2"]


def test_neg_risk_other_leg_included_in_basket() -> None:
    """A1(E): the 'Other/none-of-the-above' catch-all leg participates fully in the basket.

    The negRiskOther market is a real, tradeable partition member — if the 'Other' outcome
    wins and we didn't buy its YES, the basket pays $0.  The detector must include it as a
    leg, and its condition_id must appear in opp.condition_ids.
    """
    other_market = Market(
        id="99",
        condition_id="0xOther",
        question="None of the above?",
        outcomes=["Yes", "No"],
        clob_token_ids=["yOther", "nOther"],
        neg_risk=True,
        neg_risk_other=True,
        group_item_title="Other",
    )
    regular_markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(2)
    ]
    event = Event(
        id="9",
        title="Event with Other catch-all leg",
        neg_risk=True,
        enable_neg_risk=True,
        markets=[*regular_markets, other_market],
    )
    books = {
        "y0": make_book("y0", asks=[("0.30", "100")]),
        "y1": make_book("y1", asks=[("0.30", "100")]),
        "yOther": make_book("yOther", asks=[("0.30", "100")]),
    }
    snap = Snapshot(event=event, books=books)

    opps = list(NegRiskBasketDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    # The Other leg is a full basket participant.
    assert len(opp.legs) == 3
    assert "0xOther" in opp.condition_ids
    assert "yOther" in {leg.token_id for leg in opp.legs}


# ---------------------------------------------------------------------------
# NO-BASKET DUAL (B3) — buy 1 NO of every outcome; exactly M-1 pay → payoff M-1
# ---------------------------------------------------------------------------


def _objective_market(i: int) -> Market:
    # Void-resistant (OBJECTIVE) AND fee-free: fee_type carries "sports" → classify OBJECTIVE,
    # while fee_rate=None keeps it fee-free (fee_rate_for → 0), so the dual void-gate passes and
    # the profit assertions stay clean. The dual refuses non-OBJECTIVE legs (see refuse test).
    return Market(
        id=str(i),
        condition_id=f"0x{i}",
        question="Q?",
        outcomes=["Yes", "No"],
        clob_token_ids=[f"y{i}", f"n{i}"],
        neg_risk=True,
        fee_type="sports_fees_v2",
        group_item_title=f"O{i}",
    )


def _dual_snapshot(no_ask_prices: list[str], *, augmented: bool = False) -> Snapshot:
    markets = [_objective_market(i) for i in range(len(no_ask_prices))]
    event = Event(
        id="9",
        title="Dual event",
        neg_risk=True,
        enable_neg_risk=True,
        neg_risk_augmented=augmented,
        markets=markets,
    )
    books = {f"n{i}": make_book(f"n{i}", asks=[(p, "100")]) for i, p in enumerate(no_ask_prices)}
    return Snapshot(event=event, books=books)


def test_dual_profit_formula() -> None:
    # 3 outcomes → payoff M-1 = 2; Σ NO = 1.8 < 2 → gross 0.2.
    p = dual_profit([Decimal("0.6"), Decimal("0.6"), Decimal("0.6")], [ZERO, ZERO, ZERO])
    assert p.cost == Decimal("1.8")
    assert p.gross_profit == Decimal("0.2")
    assert p.net_profit == Decimal("0.2")


def test_dual_emits_when_no_basket_underpriced() -> None:
    snap = _dual_snapshot(["0.6", "0.6", "0.6"])  # Σ NO = 1.8 < 2
    opps = list(NegRiskDualDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.detector == DetectorKind.NEGRISK_DUAL
    assert opp.net_profit == Decimal("0.2")
    assert opp.realizes == "resolution"
    assert len(opp.legs) == 3
    assert {leg.token_id for leg in opp.legs} == {"n0", "n1", "n2"}  # NO tokens, not YES
    assert all(leg.side == "buy" for leg in opp.legs)


def test_dual_no_arb_when_sum_ge_m_minus_1() -> None:
    snap = _dual_snapshot(["0.7", "0.7", "0.7"])  # Σ NO = 2.1 ≥ 2 → no edge
    assert list(NegRiskDualDetector().detect(snap)) == []


def test_dual_works_on_augmented_event() -> None:
    # The defining YES/NO difference: the dual needs only mutual exclusivity, so it emits on an
    # augmented event (skip_augmented=False) where the YES basket correctly bails.
    snap = _dual_snapshot(["0.6", "0.6", "0.6"], augmented=True)
    assert len(list(NegRiskDualDetector().detect(snap))) == 1


def test_dual_depth_walk_captures_multiple_levels() -> None:
    markets = [_objective_market(i) for i in range(3)]
    event = make_event(markets, neg_risk=True)
    # Both NO levels profitable (payoff 2): L1 Σ=1.80<2 chunk 50; L2 Σ=1.89<2 chunk 70 → 120.
    books = {f"n{i}": make_book(f"n{i}", asks=[("0.60", "50"), ("0.63", "70")]) for i in range(3)}
    snap = Snapshot(event=event, books=books)
    opps = list(NegRiskDualDetector().detect(snap))
    assert len(opps) == 1
    assert opps[0].executable_size == Decimal(120)


def test_dual_refuses_void_prone_legs() -> None:
    # Void gate (committee CRITICAL): the dual's M-1 floor breaks if a losing leg voids 50-50,
    # so it must NOT emit when legs resolve on a non-OBJECTIVE (void-prone) source — even though
    # the same Σ NO < M-1 edge exists. Default make_market is fee-free → fee_type None → STANDARD.
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(3)
    ]
    event = make_event(markets, neg_risk=True)
    books = {f"n{i}": make_book(f"n{i}", asks=[("0.6", "100")]) for i in range(3)}  # Σ NO=1.8<2
    assert list(NegRiskDualDetector().detect(Snapshot(event=event, books=books))) == []


def test_basket_and_dual_both_fire_on_one_event() -> None:
    # Integration: an event underpriced on BOTH sides (Σ YES < 1 and Σ NO < M-1) yields one
    # YES-basket opp and one NO-dual opp from the same snapshot, each reading its own books.
    markets = [_objective_market(i) for i in range(3)]
    event = make_event(markets, neg_risk=True)
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.30", "100")]) for i in range(3)}  # Σ YES=0.9<1
    books |= {f"n{i}": make_book(f"n{i}", asks=[("0.60", "100")]) for i in range(3)}  # Σ NO=1.8<2
    snap = Snapshot(event=event, books=books)
    kinds = {opp.detector for opp in NegRiskBasketDetector().detect(snap)} | {
        opp.detector for opp in NegRiskDualDetector().detect(snap)
    }
    assert kinds == {DetectorKind.NEGRISK_BASKET, DetectorKind.NEGRISK_DUAL}


def test_dual_gas_guard() -> None:
    base = _dual_snapshot(["0.6", "0.6", "0.6"])  # net 0.2/set, size 100 → total_net 20.00
    high = Snapshot(event=base.event, books=base.books, gas=Decimal("20.01"))
    assert list(NegRiskDualDetector().detect(high)) == []
    low = Snapshot(event=base.event, books=base.books, gas=Decimal("19.99"))
    assert len(list(NegRiskDualDetector().detect(low))) == 1


def test_dual_skips_on_missing_no_book() -> None:
    snap = _dual_snapshot(["0.6", "0.6", "0.6"])
    del snap.books["n2"]  # a live outcome with no NO book → can't lock the dual
    assert list(NegRiskDualDetector().detect(snap)) == []


# ---------------------------------------------------------------------------
# B2' — gas scales by leg count (gas = base + per_leg * N)
# ---------------------------------------------------------------------------


def test_gas_for_scales_by_leg_count() -> None:
    snap = Snapshot(gas=Decimal("1.00"), gas_per_leg=Decimal("0.50"))
    assert snap.gas_for(2) == Decimal("2.00")  # 1 + 0.5*2
    assert snap.gas_for(12) == Decimal("7.00")  # 1 + 0.5*12
    assert Snapshot().gas_for(5) == ZERO  # 0/0 defaults → gas off


def test_per_leg_gas_suppresses_basket() -> None:
    # 3 outcomes @0.30: net 0.10/set, size 100 → total_net 10.00 before gas.
    base = _three_outcome_snapshot(["0.30", "0.30", "0.30"])
    # per_leg=4 → gas = 0 + 4*3 = 12 > 10 → suppressed; per_leg=3 → gas = 9 < 10 → emits.
    hi = Snapshot(event=base.event, books=base.books, gas_per_leg=Decimal("4"))
    assert list(NegRiskBasketDetector().detect(hi)) == []
    lo = Snapshot(event=base.event, books=base.books, gas_per_leg=Decimal("3"))
    assert len(list(NegRiskBasketDetector().detect(lo))) == 1
