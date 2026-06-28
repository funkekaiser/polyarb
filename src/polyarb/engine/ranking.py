"""Rank opportunities for emission.

Sort by risk-adjusted, annualized return (SPEC). Concretely: safer resolution first, then
higher net profit in basis points, then higher annualized return (instant arbs, which have no
annualized figure, are treated as effectively unbounded since they realize immediately).
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
    return (risk_rank(opp.resolution_risk), -opp.net_profit_bps, -annualized)


def rank(opps: list[Opportunity]) -> list[Opportunity]:
    """Return opportunities best-first (safest, most profitable, best annualized)."""
    return sorted(opps, key=_sort_key)
