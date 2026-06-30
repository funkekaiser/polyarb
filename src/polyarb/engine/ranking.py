"""Rank opportunities for emission.

Safer resolution first, then by **absolute total net dollars** (`size·net - gas`), then
annualized return as a tiebreak. (C3) We rank on absolute $, NOT net bps: bps rewards
cent-cost legs whose return % explodes, floating data artifacts and thin/low-volume opps to
the top (the winner's curse — ranking by a noisy estimate selects measurement error). Absolute
$ is bounded by executable depth, so real money rises and artifacts sink without an explicit
implausibility filter. Risk stays the primary axis (resolution-risk is a first-class gate,
SPEC §5); the annualized tiebreak orders held arbs by capital efficiency, with instant arbs
(no annualized figure, capital freed immediately) treated as effectively unbounded.

A fourth, LOW-PRIORITY key (A1-riskwt) nudges near-fully-resolved NegRisk baskets downward.
Late-life events with many eliminations are disproportionately stale-print-prone; a higher
live fraction (live_count / total_count) is a mild quality signal. This key:
  - is ONLY a tiebreaker — it cannot move a larger-$ or safer-tier opp below a smaller/riskier
    one, because risk and absolute $ sort first;
  - treats None (opps that don't set the field) as fully live (``-1``) so complement/dependency
    opps are not penalized and the existing ranking is preserved for non-NegRisk opps.
If a stronger or probability-weighted penalty is warranted, DEFER TO COMMITTEE — do not
implement here; a stronger penalty could violate the absolute-$ dominance invariant.
"""

from __future__ import annotations

from decimal import Decimal

from polyarb.models import Opportunity
from polyarb.resolution.risk import risk_rank

# Instant arbs realize immediately — rank them ahead of resolution arbs on the annualized axis.
_INSTANT_ANNUALIZED = Decimal(10**9)

# Neutral live-fraction key: opps without live_count/total_count are treated as fully live
# (-1) so they are not penalized by the tiebreak and the sort is stable for non-NegRisk opps.
_LIVE_FRACTION_NEUTRAL = Decimal(-1)


def _live_fraction_key(opp: Opportunity) -> Decimal:
    """Soft tiebreak (A1-riskwt): prefer baskets with more live legs (less resolved events).

    Returns the negated live fraction so ascending sort puts the most-live basket first.
    None is treated as fully live (``-1``) — neutral, no penalty for opps that don't set
    the field. This key is the LOWEST priority axis and must NEVER reorder real money:
    it only breaks ties after risk-tier and absolute-$ already agree.
    """
    if opp.live_count is not None and opp.total_count is not None and opp.total_count > 0:
        # Clamp to the neutral floor: an invalid live_count > total_count (only reachable via
        # direct construction; detectors can't produce it) must not score BETTER than a fully
        # live basket. Valid fractions are in [0, 1] → key in [-1, 0]; the floor caps it at -1.
        return max(-Decimal(opp.live_count) / Decimal(opp.total_count), _LIVE_FRACTION_NEUTRAL)
    return _LIVE_FRACTION_NEUTRAL  # neutral: treated as fully live


def _sort_key(opp: Opportunity) -> tuple[int, Decimal, Decimal, Decimal]:
    annualized = (
        _INSTANT_ANNUALIZED if opp.realizes == "instant" else (opp.annualized or Decimal(0))
    )
    # Negate the "more is better" axes so ascending sort puts the best first. The $-axis uses
    # the conservative decision_net_profit (C1-atomicity-use) so winner's-curse tail-inflated
    # size can't float an unfillable opp to the top; the optimistic total stays surfaced.
    return (
        risk_rank(opp.resolution_risk),
        -opp.decision_net_profit,
        -annualized,
        _live_fraction_key(opp),
    )


def rank(opps: list[Opportunity]) -> list[Opportunity]:
    """Return opportunities best-first (safest, most absolute net $, best annualized)."""
    return sorted(opps, key=_sort_key)
