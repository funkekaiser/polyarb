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


# UMA's default dispute-window length (seconds) for Polymarket; ``customLiveness == 0`` means
# "use this default". A market that sets a *longer-than-default* window is marginally more
# contention-prone — a weak, forward-looking signal (see classify_market).
_UMA_DEFAULT_LIVENESS_S = 7200


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
    """Map a market to a resolution-risk tag (its category via fee type, plus a weak void nudge).

    A2 (partial) — a *longer-than-default* UMA dispute window (``custom_liveness >
    _UMA_DEFAULT_LIVENESS_S``) is a weak signal of a more contention-prone resolution, so we
    rank it down (ELEVATED), not exclude it. This is deliberately mild: ``customLiveness`` is the
    dispute-window *length*, not a void probability, and on current markets it is almost always
    0 — so it neither detects nor meaningfully gates the real A2 risk (a leg resolving 50-50 /
    void, which breaks the basket's "exactly one pays $1" floor). **That core void risk remains
    open** (not reliably detectable from available data — see STRATEGY_BACKLOG A2); the only
    concrete void protection today is `live_partition` dropping already-voided *closed* legs.
    """
    if market.custom_liveness > _UMA_DEFAULT_LIVENESS_S:
        return ResolutionRisk.ELEVATED
    fee_type = (market.fee_type or "").lower()
    if any(token in fee_type for token in ("crypto", "finance", "price", "sports")):
        return ResolutionRisk.OBJECTIVE
    if "politics" in fee_type and "geopolitics" not in fee_type:
        return ResolutionRisk.ELEVATED
    # mentions/culture/economics/weather/general and fee-free geopolitics → standard.
    return ResolutionRisk.STANDARD


def aggregate_risk(markets: list[Market]) -> ResolutionRisk:
    """The worst (highest) resolution risk across the markets an opportunity spans."""
    if not markets:
        return ResolutionRisk.STANDARD
    return max((classify_market(m) for m in markets), key=lambda r: _RISK_ORDER[r])
