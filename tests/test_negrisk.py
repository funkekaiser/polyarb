"""NegRisk basket detector — under-priced basket, convert-is-not-arb, annualized."""

from __future__ import annotations

from decimal import Decimal

from polyarb.detectors.base import Snapshot
from polyarb.detectors.negrisk_basket import (
    NegRiskBasketDetector,
    basket_profit,
    negrisk_convert_pnl,
)
from tests.helpers import make_book, make_event, make_market

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
