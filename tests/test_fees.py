"""Fee model — unit + property tests."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from polyarb.pricing.fees import fee_rate_for, taker_fee
from tests.helpers import make_market

ONE = Decimal(1)
ZERO = Decimal(0)
prices = st.decimals(min_value=0, max_value=1, places=4, allow_nan=False, allow_infinity=False)
rates = st.decimals(
    min_value=0, max_value=Decimal("0.1"), places=4, allow_nan=False, allow_infinity=False
)


def test_fee_zero_at_price_extremes() -> None:
    assert taker_fee(Decimal(0), ONE, Decimal("0.07")) == 0
    assert taker_fee(ONE, ONE, Decimal("0.07")) == 0


def test_fee_zero_when_fee_free() -> None:
    assert taker_fee(Decimal("0.5"), ONE, Decimal(0)) == 0


def test_fee_peaks_at_half() -> None:
    assert taker_fee(Decimal("0.5"), ONE, Decimal("0.07")) > taker_fee(
        Decimal("0.3"), ONE, Decimal("0.07")
    )


def test_fee_rate_for_market() -> None:
    assert fee_rate_for(make_market(fee_rate=None)) == 0
    assert fee_rate_for(make_market(fee_rate=0.07)) == Decimal("0.07")


@given(p=prices, r1=rates, r2=rates)
def test_fee_monotonic_in_rate(p: Decimal, r1: Decimal, r2: Decimal) -> None:
    lo, hi = sorted([r1, r2])
    assert taker_fee(p, ONE, lo) <= taker_fee(p, ONE, hi)


@given(p=prices, r=rates)
def test_fee_nonnegative(p: Decimal, r: Decimal) -> None:
    assert taker_fee(p, ONE, r) >= 0


@given(p=prices, r=rates)
def test_fee_symmetric_around_half(p: Decimal, r: Decimal) -> None:
    assert taker_fee(p, ONE, r) == taker_fee(ONE - p, ONE, r)


def test_m3_no_fee_floor_at_longshot_prices() -> None:
    """Longshot fees follow the pure parabola with no per-order floor — M3-feefloor closed.

    Backlog item M3-feefloor: the parabolic fee ``C*r*p*(1-p)`` -> 0 as p -> 0/1, raising
    concern that a real per-order floor would make our model too optimistic on longshot legs.

    Live recon (2026-06-30): the Polymarket ``feeSchedule`` object has exactly four fields —
    ``{exponent, rate, takerOnly, rebateRate}`` — with no ``min``, ``floor``, or ``minimum``
    field. The formula is purely parabolic (``exponent=1`` confirmed across all sampled
    categories). M3 is CLOSED: no floor exists; fee → 0 at extremes is correct.
    See docs/API_NOTES.md §Fees, M3 entry.

    This test is the trip-wire: if Polymarket introduces a minimum fee in a future schedule
    update, a floor implementation would make this assertion fail (fee_longshot would no longer
    equal the raw parabola).
    """
    # p=0.01 (1-cent longshot): fee = 1 * 0.07 * 0.01 * 0.99 = 0.000693 — pure parabola
    fee_longshot = taker_fee(Decimal("0.01"), ONE, Decimal("0.07"))
    expected_parabola = Decimal("0.07") * Decimal("0.01") * Decimal("0.99")
    assert fee_longshot == expected_parabola, (
        f"Longshot fee {fee_longshot} != pure parabola {expected_parabola}; "
        "if a floor was introduced, implement it and update API_NOTES (M3 entry)."
    )
    # Confirm small-but-positive (not zero, not floored upward)
    assert ZERO < fee_longshot < taker_fee(Decimal("0.50"), ONE, Decimal("0.07"))

    # Fee-free markets remain unaffected regardless of price
    assert taker_fee(Decimal("0.01"), ONE, ZERO) == ZERO
