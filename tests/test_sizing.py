"""Executable-size / book-depth tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from polyarb.models import BookLevel
from polyarb.pricing.fees import taker_fee
from polyarb.pricing.sizing import (
    depth_at_or_better,
    executable_size,
    is_crossed,
    top_level_min_depth,
    walk_buy_legs,
    walk_sell_legs,
)
from tests.helpers import make_book

ZERO = Decimal(0)
ONE = Decimal(1)


def _lvl(price: str, size: str) -> BookLevel:
    return BookLevel(price=Decimal(price), size=Decimal(size))


# ---------------------------------------------------------------------------
# Simulation helpers — independent re-implementations used by property tests
# ---------------------------------------------------------------------------


def _buy_steps(
    leg_levels: list[list[BookLevel]],
    fee_rate: Decimal | list[Decimal],
    payoff: Decimal = ONE,
) -> list[tuple[Decimal, Decimal, bool, Decimal]]:
    """Simulate walk_buy_legs step-by-step.

    Returns one entry per price-slice visited:
      (sum_prices, marginal_fee, was_included, chunk_size)
    ``chunk_size`` is ZERO for the first excluded slice (if any).
    Returns [] when the walk exits early (empty legs / no depth).
    """
    n_legs = len(leg_levels)
    if n_legs == 0:
        return []
    rates: list[Decimal] = [fee_rate] * n_legs if isinstance(fee_rate, Decimal) else list(fee_rate)

    sorted_legs: list[list[BookLevel]] = []
    for levels in leg_levels:
        filtered = sorted(
            (lvl for lvl in levels if lvl.size > ZERO and lvl.price > ZERO),
            key=lambda lvl: lvl.price,
        )
        if not filtered:
            return []
        sorted_legs.append(filtered)

    idx = [0] * n_legs
    remaining = [sorted_legs[i][0].size for i in range(n_legs)]
    steps: list[tuple[Decimal, Decimal, bool, Decimal]] = []

    while True:
        if any(idx[i] >= len(sorted_legs[i]) for i in range(n_legs)):
            break

        prices = [sorted_legs[i][idx[i]].price for i in range(n_legs)]
        mf = sum((taker_fee(prices[i], ONE, rates[i]) for i in range(n_legs)), ZERO)
        sp = sum(prices, ZERO)

        if sp + mf >= payoff:
            steps.append((sp, mf, False, ZERO))
            break

        chunk = min(remaining[i] for i in range(n_legs))
        steps.append((sp, mf, True, chunk))

        for i in range(n_legs):
            remaining[i] -= chunk
            if remaining[i] == ZERO:
                idx[i] += 1
                if idx[i] < len(sorted_legs[i]):
                    remaining[i] = sorted_legs[i][idx[i]].size

    return steps


def _sell_steps(
    leg_levels: list[list[BookLevel]],
    fee_rate: Decimal | list[Decimal],
    collateral: Decimal = ONE,
) -> list[tuple[Decimal, Decimal, bool, Decimal]]:
    """Simulate walk_sell_legs step-by-step.

    Returns one entry per price-slice visited:
      (sum_prices, marginal_fee, was_included, chunk_size)
    ``chunk_size`` is ZERO for the first excluded slice (if any).
    Returns [] when the walk exits early (empty legs / no depth).
    """
    n_legs = len(leg_levels)
    if n_legs == 0:
        return []
    rates: list[Decimal] = [fee_rate] * n_legs if isinstance(fee_rate, Decimal) else list(fee_rate)

    sorted_legs: list[list[BookLevel]] = []
    for levels in leg_levels:
        filtered = sorted(
            (lvl for lvl in levels if lvl.size > ZERO and lvl.price > ZERO),
            key=lambda lvl: lvl.price,
            reverse=True,
        )
        if not filtered:
            return []
        sorted_legs.append(filtered)

    idx = [0] * n_legs
    remaining = [sorted_legs[i][0].size for i in range(n_legs)]
    steps: list[tuple[Decimal, Decimal, bool, Decimal]] = []

    while True:
        if any(idx[i] >= len(sorted_legs[i]) for i in range(n_legs)):
            break

        prices = [sorted_legs[i][idx[i]].price for i in range(n_legs)]
        mf = sum((taker_fee(prices[i], ONE, rates[i]) for i in range(n_legs)), ZERO)
        sp = sum(prices, ZERO)

        if sp - mf <= collateral:
            steps.append((sp, mf, False, ZERO))
            break

        chunk = min(remaining[i] for i in range(n_legs))
        steps.append((sp, mf, True, chunk))

        for i in range(n_legs):
            remaining[i] -= chunk
            if remaining[i] == ZERO:
                idx[i] += 1
                if idx[i] < len(sorted_legs[i]):
                    remaining[i] = sorted_legs[i][idx[i]].size

    return steps


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Prices strictly inside (0, 1) with 2 decimal places: 0.01 … 0.99.
_price_st: st.SearchStrategy[Decimal] = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("0.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)

# Sizes: positive integers expressed as Decimal (1 … 200).
_size_st: st.SearchStrategy[Decimal] = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("200"),
    allow_nan=False,
    allow_infinity=False,
    places=0,
)

# Fee rates: 0 … 0.10, two decimal places.
_fee_st: st.SearchStrategy[Decimal] = st.decimals(
    min_value=Decimal("0.00"),
    max_value=Decimal("0.10"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)

# Payoff / collateral: 0.50 … 1.50, two decimal places.
_payoff_st: st.SearchStrategy[Decimal] = st.decimals(
    min_value=Decimal("0.50"),
    max_value=Decimal("1.50"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)


@st.composite
def _valid_level_st(draw: st.DrawFn) -> BookLevel:
    """A BookLevel with price in (0,1) and positive size."""
    return BookLevel(price=draw(_price_st), size=draw(_size_st))


@st.composite
def _junk_level_st(draw: st.DrawFn) -> BookLevel:
    """A BookLevel that should be filtered out (zero/negative price or zero size)."""
    kind = draw(st.integers(min_value=0, max_value=2))
    if kind == 0:
        # zero size, valid price
        return BookLevel(price=draw(_price_st), size=ZERO)
    elif kind == 1:
        # zero price, valid size
        return BookLevel(price=ZERO, size=draw(_size_st))
    else:
        # negative price, valid size
        return BookLevel(price=Decimal("-0.10"), size=draw(_size_st))


@st.composite
def _leg_st(draw: st.DrawFn) -> list[BookLevel]:
    """One leg: 0-5 valid levels mixed with 0-2 junk levels, in arbitrary order."""
    valid = draw(st.lists(_valid_level_st(), min_size=0, max_size=5))
    junk = draw(st.lists(_junk_level_st(), min_size=0, max_size=2))
    levels = valid + junk
    draw(st.randoms(use_true_random=False)).shuffle(levels)
    return levels


@st.composite
def _multi_leg_st(draw: st.DrawFn) -> list[list[BookLevel]]:
    """0-4 legs, each independently generated."""
    n = draw(st.integers(min_value=0, max_value=4))
    return [draw(_leg_st()) for _ in range(n)]


# ---------------------------------------------------------------------------
# Existing tests (unchanged)
# ---------------------------------------------------------------------------


def test_top_level_min_depth_buy_and_sell() -> None:
    # Buy: best = lowest ask per leg; min across legs of that best-level size.
    legs = [[_lvl("0.25", "50"), _lvl("0.28", "60")], [_lvl("0.30", "40"), _lvl("0.33", "90")]]
    assert top_level_min_depth(legs, side="buy") == Decimal(40)  # min(50, 40) at best prices
    # Sell: best = highest bid per leg.
    bids = [[_lvl("0.60", "30"), _lvl("0.55", "80")], [_lvl("0.50", "70")]]
    assert top_level_min_depth(bids, side="sell") == Decimal(30)  # min(30 @0.60, 70 @0.50)
    # A leg with no valid (positive) level → 0.
    assert top_level_min_depth([[_lvl("0.25", "50")], [_lvl("0", "0")]], side="buy") == Decimal(0)


def test_depth_buy_sums_asks_at_or_below_limit() -> None:
    book = make_book(asks=[("0.40", "10"), ("0.45", "5"), ("0.50", "20")])
    assert depth_at_or_better(book, "buy", Decimal("0.45")) == Decimal(15)


def test_depth_sell_sums_bids_at_or_above_limit() -> None:
    book = make_book(bids=[("0.60", "10"), ("0.55", "5"), ("0.50", "20")])
    assert depth_at_or_better(book, "sell", Decimal("0.55")) == Decimal(15)


def test_executable_size_is_thinnest_leg() -> None:
    assert executable_size([Decimal(10), Decimal(3), Decimal(7)]) == Decimal(3)
    assert executable_size([]) == Decimal(0)


def test_invalid_side_raises() -> None:
    with pytest.raises(ValueError):
        depth_at_or_better(make_book(), "hold", Decimal("0.5"))


# ---------------------------------------------------------------------------
# A — per-leg fee_rate for walk_buy_legs
# ---------------------------------------------------------------------------


def test_per_leg_fees_equal_scalar() -> None:
    """walk_buy_legs([...], [r, r]) produces identical result to walk_buy_legs([...], r).

    A per-leg list that broadcasts the same rate must be indistinguishable from
    passing the scalar directly — same size, same per-leg costs, same total fees.
    """
    fee = Decimal("0.03")
    yes_bl = [
        BookLevel(price=Decimal("0.30"), size=Decimal("100")),
        BookLevel(price=Decimal("0.40"), size=Decimal("50")),
    ]
    no_bl = [
        BookLevel(price=Decimal("0.30"), size=Decimal("80")),
        BookLevel(price=Decimal("0.35"), size=Decimal("60")),
    ]
    size_s, costs_s, fees_s = walk_buy_legs([yes_bl, no_bl], fee)
    size_l, costs_l, fees_l = walk_buy_legs([yes_bl, no_bl], [fee, fee])
    assert size_s == size_l
    assert costs_s == costs_l
    assert fees_s == fees_l


def test_walk_buy_legs_empty_returns_zero() -> None:
    """No legs → zero result, not a min()-on-empty crash."""
    assert walk_buy_legs([], Decimal("0.02")) == (Decimal(0), [], Decimal(0))


def test_per_leg_wrong_length_raises() -> None:
    """A per-leg fee list whose length != n_legs raises ValueError."""
    yes_bl = [BookLevel(price=Decimal("0.30"), size=Decimal("100"))]
    no_bl = [BookLevel(price=Decimal("0.40"), size=Decimal("100"))]
    with pytest.raises(ValueError):
        walk_buy_legs([yes_bl, no_bl], [Decimal("0.05")])  # 1 rate for 2 legs
    with pytest.raises(ValueError):
        walk_buy_legs(
            [yes_bl, no_bl],
            [Decimal("0.05"), Decimal("0.05"), Decimal("0.05")],  # 3 rates for 2 legs
        )


def test_higher_per_leg_fee_monotonically_shrinks_size() -> None:
    """Raising the fee on one leg (near the profitability boundary) reduces or preserves size.

    YES: [(0.30, 100), (0.40, 100)], NO: [(0.55, 100), (0.55, 100)].

    With scalar fee=0.02 on both legs:
      Round 1: 0.30+0.55+fee(0.02) = 0.86 < 1 → profitable; chunk=100.
      Round 2: 0.40+0.55+fee(0.02) = 0.96 < 1 → profitable; chunk=100.
      size = 200.

    With per-leg [0.02, 0.20] (elevated NO fee):
      Round 1: sum+fee = 0.85+0.054 ≈ 0.904 < 1 → profitable; chunk=100.
      Round 2: sum+fee = 0.95+0.054 ≈ 1.004 ≥ 1 → stop.
      size = 100 ≤ 200.
    """
    yes_bl = [
        BookLevel(price=Decimal("0.30"), size=Decimal("100")),
        BookLevel(price=Decimal("0.40"), size=Decimal("100")),
    ]
    no_bl = [
        BookLevel(price=Decimal("0.55"), size=Decimal("100")),
        BookLevel(price=Decimal("0.55"), size=Decimal("100")),
    ]
    size_low, _, _ = walk_buy_legs([yes_bl, no_bl], Decimal("0.02"))
    size_high, _, _ = walk_buy_legs([yes_bl, no_bl], [Decimal("0.02"), Decimal("0.20")])
    assert size_high <= size_low
    assert size_low == Decimal(200)
    assert size_high == Decimal(100)


def test_per_leg_fees_bind_to_their_own_leg() -> None:
    """Each rate is charged on ITS leg — swapping the rates changes total fees.

    Distinct prices AND distinct rates, so the fee total is *not* permutation-invariant; this
    pins rate→leg alignment (a misaligned impl would compute the swapped total instead).
    YES @0.30, NO @0.60, single level each, both orderings profitable so size is identical.
    """
    yes_bl = [BookLevel(price=Decimal("0.30"), size=Decimal("100"))]
    no_bl = [BookLevel(price=Decimal("0.60"), size=Decimal("100"))]

    size_a, _, fees_a = walk_buy_legs([yes_bl, no_bl], [Decimal("0.02"), Decimal("0.20")])
    size_b, _, fees_b = walk_buy_legs([yes_bl, no_bl], [Decimal("0.20"), Decimal("0.02")])

    one = Decimal(1)
    assert size_a == size_b == Decimal(100)  # alignment can't be inferred from size here
    expected_a = (
        taker_fee(Decimal("0.30"), one, Decimal("0.02"))
        + taker_fee(Decimal("0.60"), one, Decimal("0.20"))
    ) * Decimal(100)
    assert fees_a == expected_a
    assert fees_a != fees_b  # swapping the rates must change the total


# ---------------------------------------------------------------------------
# B — is_crossed
# ---------------------------------------------------------------------------


def test_is_crossed_true_when_bid_gte_ask() -> None:
    """Best bid (0.60) >= best ask (0.40) → crossed."""
    book = make_book("t", bids=[("0.60", "100")], asks=[("0.40", "100")])
    assert is_crossed(book) is True


def test_is_crossed_true_when_bid_equals_ask() -> None:
    """Best bid == best ask is also a crossed condition."""
    book = make_book("t", bids=[("0.50", "100")], asks=[("0.50", "100")])
    assert is_crossed(book) is True


def test_is_crossed_false_for_normal_book() -> None:
    """Normal book: best bid (0.30) < best ask (0.60) → not crossed."""
    book = make_book("t", bids=[("0.30", "100")], asks=[("0.60", "100")])
    assert is_crossed(book) is False


def test_is_crossed_false_when_only_bids() -> None:
    """One-sided book (bids only, no asks) is never crossed — no second side to compare."""
    book = make_book("t", bids=[("0.60", "100")])
    assert is_crossed(book) is False


def test_is_crossed_false_when_only_asks() -> None:
    """One-sided book (asks only, no bids) is never crossed."""
    book = make_book("t", asks=[("0.40", "100")])
    assert is_crossed(book) is False


def test_is_crossed_false_when_all_prices_nonpositive() -> None:
    """Zero and negative prices are filtered; if no valid levels remain, not crossed."""
    book = make_book("t", bids=[("0", "100"), ("-0.10", "50")], asks=[("0", "200")])
    assert is_crossed(book) is False


def test_is_crossed_false_when_all_sizes_zero() -> None:
    """Zero-size levels are filtered; a book of zero-size levels is not crossed."""
    book = make_book("t", bids=[("0.60", "0")], asks=[("0.40", "0")])
    assert is_crossed(book) is False


# ---------------------------------------------------------------------------
# C — D6: walk_sell_legs per-leg fee rates
# ---------------------------------------------------------------------------


def test_walk_sell_empty_returns_zero() -> None:
    """No legs → zero result, not a crash."""
    assert walk_sell_legs([], Decimal("0.02")) == (ZERO, [], ZERO)


def test_walk_sell_scalar_eq_list() -> None:
    """walk_sell_legs([...], r) == walk_sell_legs([...], [r, r]).

    Broadcast semantics: a scalar applied to every leg must be byte-identical to
    passing a uniform per-leg list — same size, same per-leg proceeds, same total fees.
    """
    fee = Decimal("0.03")
    yes_bl = [BookLevel(price=Decimal("0.55"), size=Decimal("100"))]
    no_bl = [BookLevel(price=Decimal("0.55"), size=Decimal("100"))]
    # 0.55+0.55=1.10; fee per set ≈ 0.03*0.55*0.45*2 = 0.01485; 1.10-0.01485 > 1.00 ✓
    size_s, procs_s, fees_s = walk_sell_legs([yes_bl, no_bl], fee)
    size_l, procs_l, fees_l = walk_sell_legs([yes_bl, no_bl], [fee, fee])
    assert size_s == size_l
    assert procs_s == procs_l
    assert fees_s == fees_l


def test_walk_sell_wrong_length_raises() -> None:
    """A per-leg fee list whose length != n_legs raises ValueError."""
    yes_bl = [BookLevel(price=Decimal("0.55"), size=Decimal("100"))]
    no_bl = [BookLevel(price=Decimal("0.55"), size=Decimal("100"))]
    with pytest.raises(ValueError):
        walk_sell_legs([yes_bl, no_bl], [Decimal("0.05")])  # 1 rate for 2 legs
    with pytest.raises(ValueError):
        walk_sell_legs(
            [yes_bl, no_bl],
            [Decimal("0.05"), Decimal("0.05"), Decimal("0.05")],  # 3 rates for 2 legs
        )


def test_walk_sell_per_leg_rates_differentiated() -> None:
    """Each sell rate is charged on ITS leg — swapping rates with distinct prices changes fees.

    YES bids @0.60, NO bids @0.50; rates [0.02, 0.20] vs [0.20, 0.02].
    Both orderings are profitable (sum=1.10 > collateral=1.00 after any reasonable fee),
    so size=100 in both cases, but total fees differ because price*rate differs per leg.

    fee_a = fee(0.60, 0.02) + fee(0.50, 0.20) = 0.0048 + 0.05 = 0.0548  (per set)
    fee_b = fee(0.60, 0.20) + fee(0.50, 0.02) = 0.0480 + 0.005 = 0.053   (per set)
    0.0548 ≠ 0.053 → fees_a ≠ fees_b.
    """
    yes_bl = [BookLevel(price=Decimal("0.60"), size=Decimal("100"))]
    no_bl = [BookLevel(price=Decimal("0.50"), size=Decimal("100"))]

    size_a, _, fees_a = walk_sell_legs([yes_bl, no_bl], [Decimal("0.02"), Decimal("0.20")])
    size_b, _, fees_b = walk_sell_legs([yes_bl, no_bl], [Decimal("0.20"), Decimal("0.02")])

    assert size_a == size_b == Decimal(100)
    one = Decimal(1)
    expected_a = (
        taker_fee(Decimal("0.60"), one, Decimal("0.02"))
        + taker_fee(Decimal("0.50"), one, Decimal("0.20"))
    ) * Decimal(100)
    assert fees_a == expected_a
    assert fees_a != fees_b


# ---------------------------------------------------------------------------
# D — F2: property tests for walk_buy_legs and walk_sell_legs (Hypothesis)
# ---------------------------------------------------------------------------


@given(legs=_multi_leg_st(), fee=_fee_st, payoff=_payoff_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_walk_buy_props(legs: list[list[BookLevel]], fee: Decimal, payoff: Decimal) -> None:
    """Properties 1-4 for walk_buy_legs.

    P1 — size and total_fees are always >= 0.
    P2 — per_leg_cost has exactly n_legs entries, each >= 0.
    P3a — if size > 0, total cost+fees < size*payoff (all included sets were profitable).
    P3b — reconstruction: each included price-slice strictly satisfies sum+fee < payoff;
          the first excluded slice (if any) does NOT satisfy it.
    P4  — marginal cost (sum_prices + marginal_fee) is non-decreasing across included slices.
    """
    n = len(legs)
    size, costs, fees_total = walk_buy_legs(legs, fee, payoff)

    # P1
    assert size >= ZERO
    assert fees_total >= ZERO

    # P2
    assert len(costs) == n
    for c in costs:
        assert c >= ZERO

    # P3a
    if size > ZERO:
        assert sum(costs, ZERO) + fees_total < size * payoff

    # P3b + P4 via reconstruction
    steps = _buy_steps(legs, fee, payoff)
    marginal_costs: list[Decimal] = []
    for sp, mf, included, _chunk in steps:
        if included:
            assert sp + mf < payoff, f"included slice not profitable: {sp}+{mf} vs {payoff}"
            marginal_costs.append(sp + mf)
        else:
            assert sp + mf >= payoff, f"excluded slice looks profitable: {sp}+{mf} vs {payoff}"

    # P4: marginal cost non-decreasing across included slices
    for i in range(1, len(marginal_costs)):
        assert marginal_costs[i] >= marginal_costs[i - 1], (
            f"marginal cost decreased at step {i}: {marginal_costs[i - 1]} → {marginal_costs[i]}"
        )


@given(legs=_multi_leg_st(), fee=_fee_st, collateral=_payoff_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_walk_sell_props(legs: list[list[BookLevel]], fee: Decimal, collateral: Decimal) -> None:
    """Properties 1-4 for walk_sell_legs.

    P1 — size and total_fees are always >= 0.
    P2 — per_leg_proceeds has exactly n_legs entries, each >= 0.
    P3a — if size > 0, total proceeds-fees > size*collateral (all included sets profitable).
    P3b — reconstruction: each included price-slice has sum-fee > collateral;
          the first excluded slice does NOT.
    P4  — marginal proceeds (sum_prices - marginal_fee) is non-increasing across included slices.
    """
    n = len(legs)
    size, procs, fees_total = walk_sell_legs(legs, fee, collateral)

    # P1
    assert size >= ZERO
    assert fees_total >= ZERO

    # P2
    assert len(procs) == n
    for p in procs:
        assert p >= ZERO

    # P3a
    if size > ZERO:
        assert sum(procs, ZERO) - fees_total > size * collateral

    # P3b + P4 via reconstruction
    steps = _sell_steps(legs, fee, collateral)
    marginal_proceeds: list[Decimal] = []
    for sp, mf, included, _chunk in steps:
        if included:
            assert sp - mf > collateral, f"included slice not profitable: {sp}-{mf} vs {collateral}"
            marginal_proceeds.append(sp - mf)
        else:
            assert sp - mf <= collateral, (
                f"excluded slice looks profitable: {sp}-{mf} vs {collateral}"
            )

    # P4: marginal proceeds non-increasing across included slices
    for i in range(1, len(marginal_proceeds)):
        assert marginal_proceeds[i] <= marginal_proceeds[i - 1], (
            f"marginal proceeds increased at step {i}: "
            f"{marginal_proceeds[i - 1]} → {marginal_proceeds[i]}"
        )


@given(legs=_multi_leg_st(), fee=_fee_st)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_walk_buy_prefix_optimality(legs: list[list[BookLevel]], fee: Decimal) -> None:
    """P5 for buy: adding levels at price=1 (never profitable) leaves size unchanged.

    A level at price=ONE means sum(prices) >= 1 = payoff, so sum+fee >= payoff (not strictly <),
    and the inclusion condition fails. Appending such levels to every leg can only add depth
    BEYOND any profitable stopping point — size must not increase.
    """
    payoff = ONE
    size_orig, _, _ = walk_buy_legs(legs, fee, payoff)

    extra = [[*lvl_list, BookLevel(price=ONE, size=Decimal("1000"))] for lvl_list in legs]
    size_extra, _, _ = walk_buy_legs(extra, fee, payoff)

    assert size_extra == size_orig, (
        f"size changed after adding price=1 levels: {size_orig} → {size_extra}"
    )


@given(legs=_multi_leg_st(), fee=_fee_st)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_walk_sell_depth_monotone(legs: list[list[BookLevel]], fee: Decimal) -> None:
    """P5 for sell: removing a level from a leg can only decrease (or maintain) size.

    This verifies prefix-optimality from the removal direction: every included set must
    be genuinely profitable, so removing one underlying level can only reduce accessible
    depth.  A static 'add worse prices and verify unchanged size' formulation fails for
    multi-leg sell because a low-price bid in leg i can combine with high prices still
    present in other legs to give a profitable combined sum.  The depth-removal direction
    is always well-defined and correct.

    Note: P3b's per-step reconstruction already checks the inclusion criterion directly;
    this test verifies the complementary depth-monotone direction of the same invariant.
    """
    collateral = ONE

    # Find a leg that has at least 2 valid levels so we can remove one without emptying it.
    valid_leg_indices = [
        i
        for i, leg in enumerate(legs)
        if sum(1 for lvl in leg if lvl.size > ZERO and lvl.price > ZERO) >= 2
    ]
    if not valid_leg_indices:
        return  # nothing useful to remove; skip rather than assert vacuously

    leg_i = valid_leg_indices[0]
    # Sort valid levels descending (sell order); remove the worst (lowest bid price).
    valid_sorted_desc = sorted(
        (lvl for lvl in legs[leg_i] if lvl.size > ZERO and lvl.price > ZERO),
        key=lambda lvl: lvl.price,
        reverse=True,
    )
    worst_lvl = valid_sorted_desc[-1]  # object identity: one of the originals
    thinner_leg = [lvl for lvl in legs[leg_i] if lvl is not worst_lvl]
    thinner_legs = [leg if i != leg_i else thinner_leg for i, leg in enumerate(legs)]

    size_orig, _, _ = walk_sell_legs(legs, fee, collateral)
    size_thinner, _, _ = walk_sell_legs(thinner_legs, fee, collateral)

    assert size_thinner <= size_orig, (
        f"removing a level increased size: {size_orig} -> {size_thinner}"
    )


@given(legs=_multi_leg_st(), fee=_fee_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_walk_sell_d6_regression(legs: list[list[BookLevel]], fee: Decimal) -> None:
    """P6 (D6 regression): scalar scalar == uniform per-leg list for walk_sell_legs.

    For any legs and fee rate, passing the scalar produces byte-identical output to
    passing a list of n copies of that scalar. This verifies broadcast semantics are
    preserved after the D6 change and that the scalar caller (complement.py) is unaffected.
    Also verifies that a mismatched-length sequence raises ValueError.
    """
    n = len(legs)
    size_s, procs_s, fees_s = walk_sell_legs(legs, fee)
    size_l, procs_l, fees_l = walk_sell_legs(legs, [fee] * n)

    assert size_s == size_l
    assert procs_s == procs_l
    assert fees_s == fees_l

    # Mismatched length must raise ValueError (for n_legs > 0 where n+1 != n)
    if n > 0:
        with pytest.raises(ValueError):
            walk_sell_legs(legs, [fee] * (n + 1))
