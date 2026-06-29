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
    DIRECTIONAL = "directional"  # §5 partial basket — NOT a structural lock; an EV bet
    AT_RISK = "at_risk"  # subjective / dispute-prone — default-excluded


# UMA's default dispute-window length (seconds) for Polymarket; ``customLiveness == 0`` means
# "use this default". A market that sets a *longer-than-default* window is marginally more
# contention-prone — a weak, forward-looking signal (see classify_market).
_UMA_DEFAULT_LIVENESS_S = 7200


def _has_active_dispute(market: Market) -> bool:
    """True if any of the market's UMA resolution states signals an in-flight dispute.

    C1 — a contested resolution is a *real-time*, data-backed at-risk signal: the eventual
    outcome is genuinely uncertain, so a "guaranteed" held-to-resolution arb on it isn't. We
    match a ``disput`` substring (case-insensitive) to be robust to exact-string variation
    ("disputed", "in_dispute", …); a bare "proposed" (normal resolution flow) is NOT flagged.
    """
    return any("disput" in s.lower() for s in market.uma_resolution_statuses)


# Ordering doubles as the primary rank key (lower = preferred). DIRECTIONAL sits below every
# structural tier so a probabilistic partial basket can never outrank a guaranteed arb, but
# above AT_RISK so it survives the default `exclude_at_risk` filter (it's an explicit opt-in).
_RISK_ORDER: dict[ResolutionRisk, int] = {
    ResolutionRisk.OBJECTIVE: 0,
    ResolutionRisk.STANDARD: 1,
    ResolutionRisk.ELEVATED: 2,
    ResolutionRisk.DIRECTIONAL: 3,
    ResolutionRisk.AT_RISK: 4,
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
    """Map a market to a resolution-risk tag from its UMA state and category.

    C1 — an **active UMA dispute** (``umaResolutionStatuses`` contains a dispute) is a real,
    data-backed at-risk signal: the resolution is being contested, so a held-to-resolution arb
    on it is not actually guaranteed. Tag it AT_RISK so the default ``exclude_at_risk`` filter
    drops it. (Instant complement arbs are exempt upstream in ``resolution_risk_for`` — they
    realize before resolution, so a dispute is irrelevant to them.)

    A2 (partial) — a *longer-than-default* dispute *window* (``custom_liveness >
    _UMA_DEFAULT_LIVENESS_S``) is a much weaker, forward-looking nudge → ELEVATED (rank-down,
    not exclude). It's the window *length*, not a void probability, and ~0% of live markets set
    it; the core pre-resolution void risk remains open (STRATEGY_BACKLOG A2). The only concrete
    void protection today is `live_partition` dropping already-voided *closed* legs.
    """
    if _has_active_dispute(market):
        return ResolutionRisk.AT_RISK
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
