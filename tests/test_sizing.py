"""Executable-size / book-depth tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from polyarb.models import BookLevel
from polyarb.pricing.fees import taker_fee
from polyarb.pricing.sizing import depth_at_or_better, executable_size, is_crossed, walk_buy_legs
from tests.helpers import make_book


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
