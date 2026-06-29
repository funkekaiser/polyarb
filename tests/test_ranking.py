"""Ranking tests — safer first, then higher absolute net $ (C3), instant ahead on annualized."""

from __future__ import annotations

from decimal import Decimal

from polyarb.engine.ranking import rank
from polyarb.models import DetectorKind, Opportunity
from polyarb.resolution.risk import ResolutionRisk


def _opp(
    *,
    net: str,
    size: str,
    risk: ResolutionRisk,
    bps: str = "100",
    realizes: str = "resolution",
    annualized: str | None = None,
) -> Opportunity:
    # total_net_profit = size * net - gas; that absolute $ (not bps) is the ranking money axis.
    return Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description=f"{risk}-{size}x{net}",
        condition_ids=["0x1"],
        legs=[],
        cost=Decimal("0.90"),
        gross_profit=Decimal(net),
        fees=Decimal(0),
        gas=Decimal(0),
        net_profit=Decimal(net),
        net_profit_bps=Decimal(bps),
        executable_size=Decimal(size),
        realizes=realizes,  # type: ignore[arg-type]
        annualized=Decimal(annualized) if annualized else None,
        resolution_risk=risk,
    )


def test_safer_resolution_ranks_first_even_with_less_money() -> None:
    # Risk is the primary axis: a small-$ OBJECTIVE arb still outranks a larger-$ ELEVATED one.
    risky_big = _opp(net="0.10", size="1000", risk=ResolutionRisk.ELEVATED)  # $100
    safe_small = _opp(net="0.10", size="10", risk=ResolutionRisk.OBJECTIVE)  # $1
    assert rank([risky_big, safe_small])[0] is safe_small


def test_higher_absolute_dollars_wins_within_same_risk() -> None:
    small = _opp(net="0.10", size="50", risk=ResolutionRisk.OBJECTIVE)  # $5
    big = _opp(net="0.10", size="500", risk=ResolutionRisk.OBJECTIVE)  # $50
    assert rank([small, big])[0] is big


def test_absolute_dollars_beats_bps_artifact() -> None:
    # The C3 point: a tiny-$ leg with an exploded bps (cent-cost artifact / thin book) must NOT
    # outrank a real large-$ arb with modest bps. Same risk tier so only the money axis decides.
    artifact = _opp(net="0.10", size="1", risk=ResolutionRisk.OBJECTIVE, bps="9999")  # $0.10
    real = _opp(net="0.05", size="1000", risk=ResolutionRisk.OBJECTIVE, bps="50")  # $50
    assert rank([artifact, real])[0] is real


def test_instant_ranks_ahead_of_resolution_on_annualized() -> None:
    # Equal $ and risk → instant (capital freed immediately) wins the annualized tiebreak.
    instant = _opp(net="0.10", size="100", risk=ResolutionRisk.OBJECTIVE, realizes="instant")
    resolution = _opp(net="0.10", size="100", risk=ResolutionRisk.OBJECTIVE, annualized="5")
    assert rank([resolution, instant])[0] is instant
