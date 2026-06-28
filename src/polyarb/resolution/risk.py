"""Resolution-risk classification.

Markets resolve via the UMA oracle and can be disputed or manipulated; "logically locked"
still depends on a clean, verifiable resolution. Every opportunity carries a
``resolution_risk`` tag derived from its market category, and the default filter hard-excludes
``AT_RISK``. Objective price-feed/sports markets are lowest risk; politics is elevated (most-
watched, most-arbed, most dispute-prone — per docs/RELATIONS.md §4).
"""

from __future__ import annotations

from enum import StrEnum

from polyarb.models import Market


class ResolutionRisk(StrEnum):
    OBJECTIVE = "objective"  # price feed / sports result — near-zero dispute risk
    STANDARD = "standard"
    ELEVATED = "elevated"  # politics and the like — allowed but ranked lower
    AT_RISK = "at_risk"  # subjective / dispute-prone — default-excluded


_RISK_ORDER: dict[ResolutionRisk, int] = {
    ResolutionRisk.OBJECTIVE: 0,
    ResolutionRisk.STANDARD: 1,
    ResolutionRisk.ELEVATED: 2,
    ResolutionRisk.AT_RISK: 3,
}


def risk_rank(risk: ResolutionRisk | str | None) -> int:
    """Numeric severity for ranking (lower = safer). Unknown → STANDARD."""
    if isinstance(risk, ResolutionRisk):
        return _RISK_ORDER[risk]
    if isinstance(risk, str):
        try:
            return _RISK_ORDER[ResolutionRisk(risk)]
        except ValueError:
            return _RISK_ORDER[ResolutionRisk.STANDARD]
    return _RISK_ORDER[ResolutionRisk.STANDARD]


def classify_market(market: Market) -> ResolutionRisk:
    """Map a market's category (via its fee type) to a resolution-risk tag."""
    fee_type = (market.fee_type or "").lower()
    if any(token in fee_type for token in ("crypto", "finance", "price", "sports")):
        return ResolutionRisk.OBJECTIVE
    if "politics" in fee_type:
        return ResolutionRisk.ELEVATED
    # mentions/culture/economics/weather/general and fee-free geopolitics → standard.
    return ResolutionRisk.STANDARD


def aggregate_risk(markets: list[Market]) -> ResolutionRisk:
    """The worst (highest) resolution risk across the markets an opportunity spans."""
    if not markets:
        return ResolutionRisk.STANDARD
    return max((classify_market(m) for m in markets), key=lambda r: _RISK_ORDER[r])
