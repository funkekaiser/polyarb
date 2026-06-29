"""Complement detector — unit tests for under/over and no-false-positive."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from polyarb.detectors.base import Snapshot
from polyarb.detectors.complement import ComplementDetector, over_profit, under_profit
from polyarb.models import BookLevel
from polyarb.pricing.sizing import walk_buy_legs, walk_sell_legs
from tests.helpers import make_book, make_market

ZERO = Decimal(0)


def test_under_profit_formula() -> None:
    p = under_profit(Decimal("0.40"), Decimal("0.50"), ZERO)
    assert p.cost == Decimal("0.90")
    assert p.gross_profit == Decimal("0.10")
    assert p.net_profit == Decimal("0.10")


def test_over_profit_formula() -> None:
    p = over_profit(Decimal("0.60"), Decimal("0.55"), ZERO)
    assert p.cost == Decimal(1)
    assert p.gross_profit == Decimal("0.15")


def test_detector_emits_under() -> None:
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100")], bids=[("0.30", "100")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.40", "100")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.net_profit == Decimal("0.10")
    assert opp.executable_size == Decimal(100)
    assert opp.realizes == "instant"
    assert {leg.side for leg in opp.legs} == {"buy"}


def test_detector_emits_over() -> None:
    market = make_market(yes="Y", no="N")
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.95", "100")], bids=[("0.60", "100")]),
            "N": make_book("N", asks=[("0.95", "100")], bids=[("0.55", "100")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    assert len(opps) == 1
    assert opps[0].gross_profit == Decimal("0.15")
    assert {leg.side for leg in opps[0].legs} == {"sell"}


def test_detector_no_opportunity_when_no_arb() -> None:
    market = make_market(yes="Y", no="N")
    snap = Snapshot(
        markets=[market],
        books={
            # asks sum 1.10 (no under); bids sum 0.85 (no over)
            "Y": make_book("Y", asks=[("0.55", "100")], bids=[("0.45", "100")]),
            "N": make_book("N", asks=[("0.55", "100")], bids=[("0.40", "100")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


def test_fees_can_erase_a_thin_edge() -> None:
    # Tiny gross edge (sum 0.99) wiped out by a 7% fee near 0.5 → no emission.
    market = make_market(yes="Y", no="N", fee_rate=0.07)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.49", "100")], bids=[("0.10", "100")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "100")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


# ---------------------------------------------------------------------------
# B1 — joint depth-walk tests
# ---------------------------------------------------------------------------


def test_multilevel_under_all_profitable() -> None:
    """Walk captures both levels: YES [(0.40,100),(0.45,100)], NO [(0.50,100),(0.50,100)].

    Total cost = 200 sets x (0.40+0.50) + 100 sets x (0.45+0.50)
               = 90x100 + 95x100? No — pairs: level-1 pairing costs 0.90/set x 100 sets
                 plus level-2 pairing costs 0.95/set x 100 sets = 185 total cost.
    VWAP cost per set = 185/200 = 0.925 → net_profit = 0.075.
    total_net_profit = 200 x 0.075 = 15.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100"), ("0.45", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100"), ("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under_opps = [o for o in opps if "under" in o.description]
    assert len(under_opps) == 1
    opp = under_opps[0]
    assert opp.executable_size == Decimal("200")
    assert opp.net_profit == Decimal("0.075")
    assert opp.total_net_profit == Decimal("15")


def test_multilevel_under_deep_unprofitable_excluded() -> None:
    """Second level of YES (0.70) makes sets 101-200 unprofitable; only first 100 included.

    Level-1: 0.40+0.50=0.90/set, profitable.
    Level-2: 0.70+0.50=1.20/set ≥ 1.0 → excluded.
    executable_size=100, total_net_profit=10.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100"), ("0.70", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100"), ("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under_opps = [o for o in opps if "under" in o.description]
    assert len(under_opps) == 1
    opp = under_opps[0]
    assert opp.executable_size == Decimal("100")
    assert opp.total_net_profit == Decimal("10")


def test_multilevel_over() -> None:
    """Walk captures both bid levels: YES [(0.60,100),(0.55,100)], NO [(0.55,100),(0.50,100)].

    Level-1 proceeds: 0.60+0.55=1.15/set x 100 = 115 total per leg pair.
    Level-2 proceeds: 0.55+0.50=1.05/set x 100.
    Total proceeds = 220 over 200 sets → proceeds_ps = 1.10 → net_profit = 0.10.
    total_net_profit = 200 x 0.10 = 20.
    Asks high (0.95) so no under opportunity is emitted.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book(
                "Y",
                asks=[("0.95", "100")],
                bids=[("0.60", "100"), ("0.55", "100")],
            ),
            "N": make_book(
                "N",
                asks=[("0.95", "100")],
                bids=[("0.55", "100"), ("0.50", "100")],
            ),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    over_opps = [o for o in opps if "over" in o.description]
    assert len(over_opps) == 1
    opp = over_opps[0]
    assert opp.executable_size == Decimal("200")
    assert opp.total_net_profit == Decimal("20")


def test_zero_size_level_skipped() -> None:
    """A 0-size ask level at 0.40 is filtered; the actual fill is at 0.41.

    YES has levels: (0.40, 0) — skipped, (0.41, 100). NO has (0.50, 100).
    Per-set cost = 0.41 + 0.50 = 0.91 → net_profit = 0.09, executable_size = 100.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "0"), ("0.41", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under_opps = [o for o in opps if "under" in o.description]
    assert len(under_opps) == 1
    opp = under_opps[0]
    assert opp.executable_size == Decimal("100")
    assert opp.cost == Decimal("0.91")


# ---------------------------------------------------------------------------
# Committee safety guards (Fix 2 + Fix 3)
# ---------------------------------------------------------------------------


def test_crossed_book_suppressed() -> None:
    """A crossed YES book (bid >= ask) is stale/erroneous; the detector must emit nothing.

    YES: bids=[0.60], asks=[0.40] — crossed (bid 0.60 >= ask 0.40).
    NO:  bids=[0.10], asks=[0.50] — normal.
    Even though YES ask + NO ask = 0.90 < 1, we must not emit an under opp.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", bids=[("0.60", "100")], asks=[("0.40", "100")]),
            "N": make_book("N", bids=[("0.10", "100")], asks=[("0.50", "100")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


def test_negative_price_level_ignored() -> None:
    """A negative ask price is a bad API payload and must be filtered out.

    YES asks: [(-0.10, 100), (0.40, 100)] — the negative level must be skipped.
    NO  asks: [(0.50, 100)].
    The walker should use the 0.40 level, giving executable_size=100 and cost=0.90 per set.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("-0.10", "100"), ("0.40", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under_opps = [o for o in opps if "under" in o.description]
    assert len(under_opps) == 1
    opp = under_opps[0]
    assert opp.executable_size == Decimal("100")
    # per-set cost = 0.40 + 0.50 = 0.90
    assert opp.cost == Decimal("0.90")


# ===========================================================================
# ADVERSARIAL TESTS — third hardening pass
# ===========================================================================

ONE = Decimal(1)
ZERO = Decimal(0)

# ---------------------------------------------------------------------------
# Hypothesis strategies (integer-hundredths prices/sizes to keep Decimal exact)
# ---------------------------------------------------------------------------

# Prices strictly inside (0, 1): 0.01 to 0.99
_price_st = st.integers(1, 99).map(lambda x: Decimal(x) / Decimal(100))
# Sizes 1-500 whole shares
_size_st = st.integers(1, 500).map(Decimal)
# Fee rates: 0%, 2%, 5%, 7%
_fee_st = st.sampled_from([0, 2, 5, 7]).map(lambda x: Decimal(x) / Decimal(100))
# Gas $0-$5 in cents
_gas_st = st.integers(0, 500).map(lambda x: Decimal(x) / Decimal(100))

_level_list_st = st.lists(st.tuples(_price_st, _size_st), min_size=0, max_size=12)


# ---------------------------------------------------------------------------
# B - Boundary: strict-inequality guards
# ---------------------------------------------------------------------------


def test_boundary_under_cost_exactly_one() -> None:
    """YES ask = 0.50, NO ask = 0.50 → sum = 1.00 exactly; strict '<' must exclude."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.50", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


def test_boundary_over_proceeds_exactly_one() -> None:
    """YES bid = 0.50, NO bid = 0.50 → proceeds = 1.00 exactly; strict '>' must exclude."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", bids=[("0.50", "100")], asks=[("0.99", "100")]),
            "N": make_book("N", bids=[("0.50", "100")], asks=[("0.99", "100")]),
        },
    )
    # asks are high → no under; bids sum = 1.00 exactly → no over (fee=0, so net = 1.00 = 1)
    assert list(ComplementDetector().detect(snap)) == []


def test_boundary_gas_exactly_equals_net_profit() -> None:
    """Gas == size * net_profit → total = 0 → must NOT emit (strict > 0 required).

    YES=0.40, NO=0.50, fee=0, size=100 → net_profit=0.10/set → 10.00 total pre-gas.
    gas=10.00 → 10.00 - 10.00 = 0 → suppress.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
        gas=Decimal("10.00"),  # exactly 100 * 0.10
    )
    assert list(ComplementDetector().detect(snap)) == []


def test_boundary_gas_just_below_net_profit_emits() -> None:
    """Gas = 9.99 < 10.00 (total net) → 0.01 > 0 → must emit."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
        gas=Decimal("9.99"),
    )
    opps = list(ComplementDetector().detect(snap))
    assert len([o for o in opps if "under" in o.description]) == 1


def test_boundary_price_zero_asks_excluded() -> None:
    """A YES ask at price=0 is filtered; only the 0.40 level matters."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0", "9999"), ("0.40", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    assert under[0].executable_size == Decimal("100")
    assert under[0].cost == Decimal("0.90")


def test_boundary_price_one_ask_excluded_from_walk() -> None:
    """An ask at price=1.0 makes sum >= 1 so the walk immediately stops — no under.

    YES ask=1.0, NO ask=0.50: sum=1.50 >= 1.0. Walk does not include it.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("1.0", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


# ---------------------------------------------------------------------------
# C - Thin-leg capping
# ---------------------------------------------------------------------------


def test_thin_yes_caps_executable_size() -> None:
    """YES is the thin leg (10 shares). executable_size must be 10, not 1000."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "10")], bids=[("0.10", "5")]),
            "N": make_book("N", asks=[("0.50", "1000")], bids=[("0.10", "5")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    assert under[0].executable_size == Decimal("10")


def test_thin_no_caps_executable_size() -> None:
    """NO is the thin leg (10 shares). executable_size must be 10, not 1000."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "1000")], bids=[("0.10", "5")]),
            "N": make_book("N", asks=[("0.50", "10")], bids=[("0.10", "5")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    assert under[0].executable_size == Decimal("10")


# ---------------------------------------------------------------------------
# D - Multi-level depth-walk interleave
# ---------------------------------------------------------------------------


def test_walk_yes_exhausts_mid_no_level() -> None:
    """YES=[100@0.40, 100@0.45], NO=[150@0.50].

    Walk pairs 100 sets at (0.40,0.50), then 50 sets at (0.45,0.50); NO has leftover 50.
    Size=150. YES cost=100*0.40+50*0.45=62.5. NO cost=150*0.50=75.
    total_net_profit = 150 - 62.5 - 75 = 12.5.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100"), ("0.45", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "150")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    opp = under[0]
    assert opp.executable_size == Decimal("150")
    assert opp.total_net_profit == Decimal("12.5")


def test_walk_second_no_level_unprofitable_stops_early() -> None:
    """YES=[100@0.40, 100@0.45], NO=[50@0.50, 100@0.60].

    Round 1: (0.40,0.50) sum=0.90 < 1; chunk=50. YES rem=50, NO→0.60, rem=100.
    Round 2: (0.40,0.60) sum=1.00 ≥ 1; stop.
    Only 50 sets included. total_net = 50*(1-0.40-0.50) = 5.0.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100"), ("0.45", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "50"), ("0.60", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    opp = under[0]
    assert opp.executable_size == Decimal("50")
    assert opp.total_net_profit == Decimal("5.0")


def test_walk_profitable_depth_continues_after_leg_advance() -> None:
    """Walk must NOT stop after YES exhausts its first level — it should continue.

    YES=[50@0.40, 100@0.45], NO=[80@0.50].
    Round 1: (0.40,0.50) chunk=50. YES→0.45, rem=100. NO rem=30.
    Round 2: (0.45,0.50) sum=0.95 < 1; chunk=30. Size=80. NO exhausted.
    YES cost=50*0.40+30*0.45=33.5. NO cost=80*0.50=40. total_net=80-73.5=6.5.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "50"), ("0.45", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "80")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    opp = under[0]
    assert opp.executable_size == Decimal("80")
    assert opp.total_net_profit == Decimal("6.5")


# ---------------------------------------------------------------------------
# E - Duplicate price levels / many levels
# ---------------------------------------------------------------------------


def test_walk_duplicate_price_levels_same_as_merged() -> None:
    """Two levels at same price produce identical result to one merged level."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap_split = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "50"), ("0.40", "50")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    snap_merged = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    opps_split = [o for o in ComplementDetector().detect(snap_split) if "under" in o.description]
    opps_merged = [o for o in ComplementDetector().detect(snap_merged) if "under" in o.description]
    assert len(opps_split) == 1 and len(opps_merged) == 1
    assert opps_split[0].executable_size == opps_merged[0].executable_size
    assert opps_split[0].total_net_profit == opps_merged[0].total_net_profit


def test_walk_many_tiny_levels_terminates_and_correct() -> None:
    """5000 identical tiny levels per leg: must terminate and compute correct total."""
    n = 5000
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "1")] * n, bids=[]),
            "N": make_book("N", asks=[("0.50", "1")] * n, bids=[]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    opp = under[0]
    assert opp.executable_size == Decimal(n)
    assert opp.total_net_profit == Decimal(n) * Decimal("0.10")


# ---------------------------------------------------------------------------
# F - Fee-rate interactions
# ---------------------------------------------------------------------------


def test_high_fee_does_not_erase_profitable_under() -> None:
    """fee=0.07, YES=0.45, NO=0.45: gross=0.10, fee≈0.03465 → net≈0.065 > 0. Must emit."""
    fee_rate = Decimal("0.07")
    p = Decimal("0.45")
    expected_fee_ps = 2 * fee_rate * p * (ONE - p)
    market = make_market(yes="Y", no="N", fee_rate=0.07)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.45", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.45", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    opp = under[0]
    assert opp.fees == expected_fee_ps
    assert opp.net_profit == ONE - p - p - expected_fee_ps


def test_high_fee_erases_marginal_under() -> None:
    """fee=0.07, YES=0.49, NO=0.50: gross=0.01, fee≈0.035 → net<0. Must not emit."""
    market = make_market(yes="Y", no="N", fee_rate=0.07)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.49", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


# ---------------------------------------------------------------------------
# G - Manual VWAP exactness (profit = independently-computed realizable dollars)
# ---------------------------------------------------------------------------


def test_under_total_net_profit_exact_multi_level() -> None:
    """VWAP accounting is exact: verify against per-level set-by-set calculation.

    YES asks: [(0.30,100), (0.40,100)], NO asks: [(0.50,100), (0.55,100)], fee=0.

    Walk:
      Round 1: (0.30,0.50) sum=0.80; chunk=100.
      Round 2: (0.40,0.55) sum=0.95; chunk=100.
    YES cost=100*0.30+100*0.40=70. NO cost=100*0.50+100*0.55=105.
    total_net_profit = 200 - 70 - 105 = 25.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.30", "100"), ("0.40", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100"), ("0.55", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    opp = under[0]
    assert opp.executable_size == Decimal("200")
    assert opp.total_net_profit == Decimal("25")
    # VWAP: (70+105)/200 = 0.875
    assert opp.cost == Decimal("175") / Decimal("200")
    assert opp.net_profit == Decimal("0.125")


def test_over_total_net_profit_exact_multi_level() -> None:
    """Exact multi-level over accounting, fee=0.

    YES bids: [(0.60,100),(0.55,100)], NO bids: [(0.55,100),(0.50,100)].
    Walk (descending):
      Round 1: (0.60,0.55) sum=1.15 > 1; chunk=100.
      Round 2: (0.55,0.50) sum=1.05 > 1; chunk=100.
    YES proceeds=115, NO proceeds=105. total_net = 220-200 = 20.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", bids=[("0.60", "100"), ("0.55", "100")], asks=[("0.95", "100")]),
            "N": make_book("N", bids=[("0.55", "100"), ("0.50", "100")], asks=[("0.95", "100")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    over_opps = [o for o in opps if "over" in o.description]
    assert len(over_opps) == 1
    opp = over_opps[0]
    assert opp.executable_size == Decimal("200")
    assert opp.total_net_profit == Decimal("20")
    assert opp.net_profit == Decimal("0.10")


def test_under_with_fee_total_net_profit_exact() -> None:
    """Verify exact total_net_profit for a multi-level under trade with fees.

    YES asks: [(0.30,100),(0.40,100)], NO asks: [(0.40,100),(0.45,100)], fee=0.05.

    Set-by-set:
      Round 1: YES=0.30, NO=0.40. fee=0.05*0.30*0.70 + 0.05*0.40*0.60 = 0.0105+0.0120=0.0225.
               net=1-0.70-0.0225=0.2775. total for 100 sets = 27.75.
      Round 2: YES=0.40, NO=0.45. fee=0.05*0.40*0.60+0.05*0.45*0.55=0.0120+0.0124=0.0244 (approx).
    """
    market = make_market(yes="Y", no="N", fee_rate=0.05)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.30", "100"), ("0.40", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.40", "100"), ("0.45", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    opp = under[0]

    # Independent calculation via walk
    fee_rate = Decimal("0.05")
    yes_bl = [
        BookLevel(price=Decimal("0.30"), size=Decimal("100")),
        BookLevel(price=Decimal("0.40"), size=Decimal("100")),
    ]
    no_bl = [
        BookLevel(price=Decimal("0.40"), size=Decimal("100")),
        BookLevel(price=Decimal("0.45"), size=Decimal("100")),
    ]
    w_size, w_costs, w_fees = walk_buy_legs([yes_bl, no_bl], fee_rate)

    expected_total_net = w_size - sum(w_costs, ZERO) - w_fees
    assert opp.executable_size == w_size
    assert opp.total_net_profit == expected_total_net


# ---------------------------------------------------------------------------
# H - Adversarial: can under+over ever BOTH emit on one non-crossed market?
# ---------------------------------------------------------------------------


@given(
    yes_ask=_price_st,
    no_ask=_price_st,
    yes_bid=st.integers(1, 98).map(lambda x: Decimal(x) / Decimal(100)),
    no_bid=st.integers(1, 98).map(lambda x: Decimal(x) / Decimal(100)),
    size=_size_st,
)
@settings(max_examples=300)
def test_hypothesis_under_over_mutually_exclusive(yes_ask, no_ask, yes_bid, no_bid, size) -> None:
    """Under and over cannot both emit on a non-crossed book (strict math guarantee).

    On a non-crossed book: bid_Y < ask_Y and bid_N < ask_N.
    Therefore bid_Y+bid_N < ask_Y+ask_N.
    If ask_Y+ask_N < 1 (under) then bid_Y+bid_N < 1 (over impossible).
    """
    # Ensure non-crossed: bids strictly below asks
    if yes_bid >= yes_ask or no_bid >= no_ask:
        return  # skip; not testing crossed books here

    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book(
                "Y",
                asks=[(str(yes_ask), str(size))],
                bids=[(str(yes_bid), str(size))],
            ),
            "N": make_book(
                "N",
                asks=[(str(no_ask), str(size))],
                bids=[(str(no_bid), str(size))],
            ),
        },
        gas=ZERO,
    )
    opps = list(ComplementDetector().detect(snap))
    unders = [o for o in opps if "under" in o.description]
    overs = [o for o in opps if "over" in o.description]
    assert not (unders and overs), (
        f"Both under AND over emitted on non-crossed book: "
        f"yes_ask={yes_ask}, no_ask={no_ask}, yes_bid={yes_bid}, no_bid={no_bid}"
    )


# ---------------------------------------------------------------------------
# I - Hypothesis: walk size never exceeds thinnest total depth
# ---------------------------------------------------------------------------


@given(yes_levels=_level_list_st, no_levels=_level_list_st, fee_int=st.integers(0, 7))
@settings(max_examples=400)
def test_hypothesis_walk_buy_size_le_min_total_depth(yes_levels, no_levels, fee_int) -> None:
    """walk_buy_legs: reported size ≤ min total depth of either leg."""
    fee_rate = Decimal(fee_int) / Decimal(100)
    yes_bl = [BookLevel(price=p, size=s) for p, s in yes_levels]
    no_bl = [BookLevel(price=p, size=s) for p, s in no_levels]
    size, _leg_costs, _fees = walk_buy_legs([yes_bl, no_bl], fee_rate)

    total_yes = sum(s for _, s in yes_levels)
    total_no = sum(s for _, s in no_levels)
    assert size <= total_yes, f"walk_buy size={size} > total YES depth={total_yes}"
    assert size <= total_no, f"walk_buy size={size} > total NO depth={total_no}"


@given(yes_levels=_level_list_st, no_levels=_level_list_st, fee_int=st.integers(0, 7))
@settings(max_examples=400)
def test_hypothesis_walk_sell_size_le_min_total_depth(yes_levels, no_levels, fee_int) -> None:
    """walk_sell_legs: reported size ≤ min total depth of either leg."""
    fee_rate = Decimal(fee_int) / Decimal(100)
    yes_bl = [BookLevel(price=p, size=s) for p, s in yes_levels]
    no_bl = [BookLevel(price=p, size=s) for p, s in no_levels]
    size, _leg_proceeds, _fees = walk_sell_legs([yes_bl, no_bl], fee_rate)

    total_yes = sum(s for _, s in yes_levels)
    total_no = sum(s for _, s in no_levels)
    assert size <= total_yes
    assert size <= total_no


# ---------------------------------------------------------------------------
# J - Hypothesis: detector opp total_net_profit matches direct walk computation
# ---------------------------------------------------------------------------


@given(
    yes_levels=_level_list_st,
    no_levels=_level_list_st,
    fee_int=st.integers(0, 7),
    gas_cents=_gas_st,
)
@settings(max_examples=500)
def test_hypothesis_detector_under_profit_matches_walk(
    yes_levels, no_levels, fee_int, gas_cents
) -> None:
    """Detector under opp total_net_profit == size - Σcosts - Σfees - gas (walk ground truth).

    Bids are set to 0.005, safely below the minimum price from strategy (0.01),
    so no book is ever crossed.
    """
    fee_rate = Decimal(fee_int) / Decimal(100)
    fee_rate_float = fee_int / 100

    market = make_market(yes="Y", no="N", fee_rate=fee_rate_float if fee_int > 0 else None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book(
                "Y",
                asks=[(str(p), str(s)) for p, s in yes_levels],
                bids=[("0.005", "1")],  # below min ask (0.01); never crossed
            ),
            "N": make_book(
                "N",
                asks=[(str(p), str(s)) for p, s in no_levels],
                bids=[("0.005", "1")],
            ),
        },
        gas=gas_cents,
    )

    # Ground truth from walk
    yes_bl = [BookLevel(price=p, size=s) for p, s in yes_levels]
    no_bl = [BookLevel(price=p, size=s) for p, s in no_levels]
    w_size, w_costs, w_fees = walk_buy_legs([yes_bl, no_bl], fee_rate)

    opps = list(ComplementDetector().detect(snap))
    under_opps = [o for o in opps if "under" in o.description]

    if w_size <= ZERO:
        # No profitable walk depth → detector must not emit
        assert len(under_opps) == 0
        return

    net_ps = ONE - sum(w_costs, ZERO) / w_size - w_fees / w_size
    expected_total_net = w_size * net_ps - gas_cents

    if expected_total_net <= ZERO:
        # Gas wiped it out → detector must not emit
        assert len(under_opps) == 0
    else:
        # Profitable → detector must emit exactly once with matching profit
        assert len(under_opps) == 1
        opp = under_opps[0]
        diff = abs(opp.total_net_profit - expected_total_net)
        assert diff < Decimal("1E-10"), (
            f"total_net_profit={opp.total_net_profit} != expected={expected_total_net} "
            f"(diff={diff})"
        )
        assert opp.executable_size == w_size


@given(
    yes_levels=_level_list_st,
    no_levels=_level_list_st,
    fee_int=st.integers(0, 7),
    gas_cents=_gas_st,
)
@settings(max_examples=500)
def test_hypothesis_detector_over_profit_matches_walk(
    yes_levels, no_levels, fee_int, gas_cents
) -> None:
    """Detector over opp total_net_profit == Σproceeds - size - Σfees - gas (walk ground truth).

    Asks are set to 0.995, safely above the maximum price from strategy (0.99),
    so no book is ever crossed.
    """
    fee_rate = Decimal(fee_int) / Decimal(100)
    fee_rate_float = fee_int / 100

    market = make_market(yes="Y", no="N", fee_rate=fee_rate_float if fee_int > 0 else None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book(
                "Y",
                bids=[(str(p), str(s)) for p, s in yes_levels],
                asks=[("0.995", "1")],  # above max bid (0.99); never crossed
            ),
            "N": make_book(
                "N",
                bids=[(str(p), str(s)) for p, s in no_levels],
                asks=[("0.995", "1")],
            ),
        },
        gas=gas_cents,
    )

    # Ground truth from walk
    yes_bl = [BookLevel(price=p, size=s) for p, s in yes_levels]
    no_bl = [BookLevel(price=p, size=s) for p, s in no_levels]
    w_size, w_proceeds, w_fees = walk_sell_legs([yes_bl, no_bl], fee_rate)

    opps = list(ComplementDetector().detect(snap))
    over_opps = [o for o in opps if "over" in o.description]

    if w_size <= ZERO:
        assert len(over_opps) == 0
        return

    proceeds_ps = sum(w_proceeds, ZERO) / w_size
    net_ps = proceeds_ps - ONE - w_fees / w_size
    expected_total_net = w_size * net_ps - gas_cents

    if expected_total_net <= ZERO:
        assert len(over_opps) == 0
    else:
        assert len(over_opps) == 1
        opp = over_opps[0]
        diff = abs(opp.total_net_profit - expected_total_net)
        assert diff < Decimal("1E-10"), (
            f"total_net_profit={opp.total_net_profit} != expected={expected_total_net} "
            f"(diff={diff})"
        )
        assert opp.executable_size == w_size


# ---------------------------------------------------------------------------
# K - Adversarial edge: walk_buy emitting on per-set profitable when overall size is wrong
# ---------------------------------------------------------------------------


def test_walk_buy_individual_set_profitable_check() -> None:
    """Every set included by the walk must have been individually profitable.

    YES asks: [(0.40, 100), (0.70, 100)], NO asks: [(0.50, 100)].
    Sets 1-100: cost=0.40+0.50=0.90 < 1 → profitable.
    Sets 101-200 would cost 0.70+0.50=1.20 ≥ 1 → not profitable.
    Walk must return size=100, NOT 200.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100"), ("0.70", "100")], bids=[("0.10", "50")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "50")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    under = [o for o in opps if "under" in o.description]
    assert len(under) == 1
    # Only the first 100 sets (all at 0.90/set cost) are included
    assert under[0].executable_size == Decimal("100")
    # Net profit must reflect ONLY the profitable 100 sets
    assert under[0].total_net_profit == Decimal("10")


def test_walk_sell_individual_set_profitable_check() -> None:
    """Every set included by the over walk must have been individually profitable.

    YES bids: [(0.60,100),(0.40,100)], NO bids: [(0.55,100)].
    Sets 1-100: proceeds=0.60+0.55=1.15 > 1 → profitable.
    Sets 101-200: proceeds=0.40+0.55=0.95 ≤ 1 → not profitable.
    Walk must return size=100.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", bids=[("0.60", "100"), ("0.40", "100")], asks=[("0.95", "100")]),
            "N": make_book("N", bids=[("0.55", "100")], asks=[("0.95", "100")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    over_opps = [o for o in opps if "over" in o.description]
    assert len(over_opps) == 1
    assert over_opps[0].executable_size == Decimal("100")
    assert over_opps[0].total_net_profit == Decimal("15")


def test_walk_size_zero_emits_nothing() -> None:
    """When walk returns size=0 (e.g. all asks sum >= 1), no opp emitted."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            # Under: sum=1.00 exactly → no under. Over: bids sum=0.85 → no over.
            "Y": make_book("Y", asks=[("0.50", "100")], bids=[("0.45", "100")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.40", "100")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []
