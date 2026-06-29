"""Rank opportunities for emission.

Safer resolution first, then by **absolute total net dollars** (`size·net - gas`), then
annualized return as a tiebreak. (C3) We rank on absolute $, NOT net bps: bps rewards
cent-cost legs whose return % explodes, floating data artifacts and thin/low-volume opps to
the top (the winner's curse — ranking by a noisy estimate selects measurement error). Absolute
$ is bounded by executable depth, so real money rises and artifacts sink without an explicit
implausibility filter. Risk stays the primary axis (resolution-risk is a first-class gate,
SPEC §5); the annualized tiebreak orders held arbs by capital efficiency, with instant arbs
(no annualized figure, capital freed immediately) treated as effectively unbounded.
"""

from __future__ import annotations

from decimal import Decimal

from polyarb.models import Opportunity
from polyarb.resolution.risk import risk_rank

# Instant arbs realize immediately — rank them ahead of resolution arbs on the annualized axis.
_INSTANT_ANNUALIZED = Decimal(10**9)


def _sort_key(opp: Opportunity) -> tuple[int, Decimal, Decimal]:
    annualized = (
        _INSTANT_ANNUALIZED if opp.realizes == "instant" else (opp.annualized or Decimal(0))
    )
    # Negate the "more is better" axes so ascending sort puts the best first.
    return (risk_rank(opp.resolution_risk), -opp.total_net_profit, -annualized)


def rank(opps: list[Opportunity]) -> list[Opportunity]:
    """Return opportunities best-first (safest, most absolute net $, best annualized)."""
    return sorted(opps, key=_sort_key)
