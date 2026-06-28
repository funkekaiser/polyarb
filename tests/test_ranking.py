"""Ranking tests — safer first, then higher bps, instant ahead of resolution."""

from __future__ import annotations

from decimal import Decimal

from polyarb.engine.ranking import rank
from polyarb.models import DetectorKind, Opportunity
from polyarb.resolution.risk import ResolutionRisk


def _opp(
    *,
    bps: str,
    risk: ResolutionRisk,
    realizes: str = "resolution",
    annualized: str | None = None,
) -> Opportunity:
    return Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description=f"{risk}-{bps}",
        condition_ids=["0x1"],
        legs=[],
        cost=Decimal("0.90"),
        gross_profit=Decimal("0.10"),
        fees=Decimal(0),
        gas=Decimal(0),
        net_profit=Decimal("0.10"),
        net_profit_bps=Decimal(bps),
        executable_size=Decimal("100"),
        realizes=realizes,  # type: ignore[arg-type]
        annualized=Decimal(annualized) if annualized else None,
        resolution_risk=risk,
    )


def test_safer_resolution_ranks_first() -> None:
    risky = _opp(bps="900", risk=ResolutionRisk.ELEVATED)
    safe = _opp(bps="100", risk=ResolutionRisk.OBJECTIVE)
    assert rank([risky, safe])[0] is safe


def test_higher_bps_wins_within_same_risk() -> None:
    low = _opp(bps="100", risk=ResolutionRisk.OBJECTIVE)
    high = _opp(bps="500", risk=ResolutionRisk.OBJECTIVE)
    assert rank([low, high])[0] is high


def test_instant_ranks_ahead_of_resolution_on_annualized() -> None:
    instant = _opp(bps="100", risk=ResolutionRisk.OBJECTIVE, realizes="instant")
    resolution = _opp(bps="100", risk=ResolutionRisk.OBJECTIVE, annualized="5")
    assert rank([resolution, instant])[0] is instant
