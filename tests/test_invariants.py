"""Property-based invariants for the arb math (hypothesis).

Proves, across random price vectors, that each profit formula matches SPEC.md §"The math"
and that a detector NEVER reports profit when the underlying identity is not violated
("no false positive"). Also pins the NegRisk convert-is-not-arb invariant.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from polyarb.detectors.base import Snapshot
from polyarb.detectors.complement import ComplementDetector, over_profit, under_profit
from polyarb.detectors.dependency import dependency_profit
from polyarb.detectors.negrisk_basket import basket_profit, negrisk_convert_pnl
from tests.helpers import make_book, make_market

ZERO = Decimal(0)
ONE = Decimal(1)

price = st.decimals(min_value=0, max_value=1, places=4, allow_nan=False, allow_infinity=False)
rate = st.decimals(
    min_value=0, max_value=Decimal("0.1"), places=4, allow_nan=False, allow_infinity=False
)
gas = st.decimals(
    min_value=0, max_value=Decimal("0.05"), places=4, allow_nan=False, allow_infinity=False
)


@given(a_yes=price, a_no=price, r=rate, g=gas)
def test_complement_under_identity_and_no_false_positive(
    a_yes: Decimal, a_no: Decimal, r: Decimal, g: Decimal
) -> None:
    p = under_profit(a_yes, a_no, r, g)
    assert p.gross_profit == ONE - (a_yes + a_no)  # exact formula
    assert p.net_profit <= p.gross_profit  # fees + gas never increase profit
    if p.net_profit > ZERO:
        assert a_yes + a_no < ONE  # profit ⇒ identity violated


@given(b_yes=price, b_no=price, r=rate, g=gas)
def test_complement_over_identity_and_no_false_positive(
    b_yes: Decimal, b_no: Decimal, r: Decimal, g: Decimal
) -> None:
    p = over_profit(b_yes, b_no, r, g)
    assert p.gross_profit == (b_yes + b_no) - ONE
    if p.net_profit > ZERO:
        assert b_yes + b_no > ONE


@given(asks=st.lists(price, min_size=3, max_size=6), g=gas)
def test_basket_identity_and_no_false_positive(asks: list[Decimal], g: Decimal) -> None:
    p = basket_profit(asks, [ZERO] * len(asks), g)
    assert p.gross_profit == ONE - sum(asks, ZERO)
    if p.net_profit > ZERO:
        assert sum(asks, ZERO) < ONE


@given(a_yes_b=price, a_no_a=price, rb=rate, ra=rate, g=gas)
def test_dependency_identity_and_no_false_positive(
    a_yes_b: Decimal, a_no_a: Decimal, rb: Decimal, ra: Decimal, g: Decimal
) -> None:
    p = dependency_profit(a_yes_b, a_no_a, rb, ra, g)
    assert p.gross_profit == ONE - (a_yes_b + a_no_a)
    if p.net_profit > ZERO:
        assert a_yes_b + a_no_a < ONE


@given(prices=st.lists(price, max_size=8))
def test_negrisk_convert_is_never_profitable(prices: list[Decimal]) -> None:
    assert negrisk_convert_pnl(prices) == ZERO


@given(a_yes=price, a_no=price, b_yes=price, b_no=price)
def test_complement_detector_only_emits_real_arbs(
    a_yes: Decimal, a_no: Decimal, b_yes: Decimal, b_no: Decimal
) -> None:
    market = make_market(yes="Y", no="N")
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[(str(a_yes), "10")], bids=[(str(b_yes), "10")]),
            "N": make_book("N", asks=[(str(a_no), "10")], bids=[(str(b_no), "10")]),
        },
    )
    for opp in ComplementDetector().detect(snap):
        # Every emitted opportunity is genuinely profitable (and thus identity-violating).
        assert opp.gross_profit > ZERO
        assert opp.net_profit > ZERO
