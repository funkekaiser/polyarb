"""Fee model — unit + property tests."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from polyarb.pricing.fees import fee_rate_for, taker_fee
from tests.helpers import make_market

ONE = Decimal(1)
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
