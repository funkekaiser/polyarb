"""Partial NegRisk basket (§5) — opt-in directional detector tests."""

from __future__ import annotations

from decimal import Decimal

from polyarb.detectors.base import Snapshot
from polyarb.detectors.partial_basket import PartialBasketDetector
from polyarb.models import DetectorKind, Market
from tests.helpers import make_book, make_event

ZERO = Decimal(0)


def _market(i: int, *, best_ask: Decimal | None = None) -> Market:
    return Market(
        id=str(i),
        condition_id=f"0x{i}",
        question="Q?",
        outcomes=["Yes", "No"],
        clob_token_ids=[f"y{i}", f"n{i}"],
        neg_risk=True,
        group_item_title=f"O{i}",
        best_ask=best_ask,
    )


def _snap(buyable_asks: list[str], unbuyable_best_asks: list[str | None]) -> Snapshot:
    """Build a negRisk event: ``buyable_asks`` get YES books; ``unbuyable_best_asks`` markets have
    no book (unfillable) but carry a Gamma cached bestAsk (or None to leave it unpriced)."""
    buyable = [_market(i) for i in range(len(buyable_asks))]
    unbuyable = [
        _market(len(buyable_asks) + j, best_ask=(Decimal(p) if p is not None else None))
        for j, p in enumerate(unbuyable_best_asks)
    ]
    event = make_event([*buyable, *unbuyable], neg_risk=True)
    books = {f"y{i}": make_book(f"y{i}", asks=[(ask, "100")]) for i, ask in enumerate(buyable_asks)}
    return Snapshot(event=event, books=books)


def test_partial_emits_on_unfillable_structural_arb() -> None:
    # 2 buyable @0.30 + 1 unbuyable (cached bestAsk 0.30): T=0.90<1 (a real structural arb that
    # can't be fully locked). Buyable subset S={y0,y1}, Σ_S=0.60, p=0.60/0.90≈0.667 → EV>0.
    snap = _snap(["0.30", "0.30"], ["0.30"])
    opps = list(PartialBasketDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.detector == DetectorKind.PARTIAL_BASKET
    assert {leg.token_id for leg in opp.legs} == {"y0", "y1"}  # only the buyable subset
    assert opp.realizes == "resolution"
    # EV/set = p - cost_ps = 0.60/0.90 - 0.60 ≈ 0.0667 (fee-free).
    assert opp.net_profit == Decimal("0.60") / Decimal("0.90") - Decimal("0.60")
    assert opp.net_profit > ZERO


def test_partial_skips_when_full_basket_buyable() -> None:
    # Every live leg buyable → that's a structural NegRisk basket; the partial detector defers.
    snap = _snap(["0.30", "0.30", "0.30"], [])
    assert list(PartialBasketDetector().detect(snap)) == []


def test_partial_skips_when_no_slack() -> None:
    # T = 1.35 ≥ 1: the full set isn't underpriced, so there's no structural arb to salvage —
    # buying a subset would be pure directional speculation. Refuse.
    snap = _snap(["0.45", "0.45"], ["0.45"])
    assert list(PartialBasketDetector().detect(snap)) == []


def test_partial_skips_when_residual_unpriced() -> None:
    # Unbuyable leg has no cached bestAsk → can't estimate the residual mass → don't bet blind.
    snap = _snap(["0.30", "0.30"], [None])
    assert list(PartialBasketDetector().detect(snap)) == []


def test_partial_skips_when_under_two_buyable() -> None:
    # Only 1 buyable leg + 2 unbuyable: a single-leg directional bet is not emitted.
    snap = _snap(["0.30"], ["0.30", "0.30"])
    assert list(PartialBasketDetector().detect(snap)) == []


def test_partial_skips_when_residual_priced_zero() -> None:
    # Gamma sends "0" (not null) for an empty book. A 0 residual must NOT pass the price guard —
    # otherwise total == Σ_S, p == 1, and the partial would fake full coverage of the subset.
    snap = _snap(["0.30", "0.30"], ["0"])
    assert list(PartialBasketDetector().detect(snap)) == []


def test_partial_tagged_directional() -> None:
    # resolution_risk_for tags a partial basket DIRECTIONAL (ranks below every structural arb).
    from polyarb.detectors.base import Profit, make_opportunity
    from polyarb.engine.scanner import resolution_risk_for
    from polyarb.resolution.risk import ResolutionRisk

    opp = make_opportunity(
        detector=DetectorKind.PARTIAL_BASKET,
        description="p",
        condition_ids=["0x0"],
        legs=[],
        profit=Profit(cost=Decimal("0.6"), gross_profit=Decimal("0.06"), fees=ZERO),
        executable_size=Decimal(1),
        realizes="resolution",
    )
    assert resolution_risk_for(opp, {}) == ResolutionRisk.DIRECTIONAL
