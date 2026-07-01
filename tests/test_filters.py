"""Emission filter + dedupe tests."""

from __future__ import annotations

from decimal import Decimal

from polyarb.config import Settings
from polyarb.engine.filters import DedupeCache, OpportunityFilter, opportunity_key
from polyarb.models import DetectorKind, Leg, Opportunity
from polyarb.resolution.risk import ResolutionRisk


def _opp(
    *,
    bps: str = "200",
    cost: str = "0.90",
    size: str = "100",
    conservative: str | None = None,
    risk: ResolutionRisk = ResolutionRisk.OBJECTIVE,
    conditions: list[str] | None = None,
    annualized: str | None = None,
    legs: list[Leg] | None = None,
) -> Opportunity:
    return Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description="t",
        condition_ids=conditions or ["0x1"],
        legs=legs or [],
        cost=Decimal(cost),
        gross_profit=Decimal("0.10"),
        fees=Decimal(0),
        gas=Decimal(0),
        net_profit=Decimal("0.10"),
        net_profit_bps=Decimal(bps),
        executable_size=Decimal(size),
        conservative_size=Decimal(conservative) if conservative is not None else None,
        realizes="instant" if annualized is None else "resolution",
        annualized=Decimal(annualized) if annualized is not None else None,
        resolution_risk=risk,
    )


def test_notional_gate_uses_conservative_size() -> None:
    """C1-atomicity-use: a fat executable_size that clears $50 is REJECTED when the conservative
    (best-level) size is sub-floor — phantom deep depth can't fake the MIN_NOTIONAL gate."""
    filt = OpportunityFilter(_settings(min_notional_usdc=Decimal(50)))
    # executable 1000 * 0.90 = $900 (passes optimistically); conservative 5 * 0.90 = $4.50 (fails).
    assert filt.apply([_opp(size="1000", conservative="5", cost="0.90")]) == []
    assert filt.stats.below_notional == 1


def test_notional_gate_zero_conservative_rejected_not_fallback() -> None:
    """is-None correctness: a Decimal(0) conservative size must NOT fall back to optimistic."""
    filt = OpportunityFilter(_settings(min_notional_usdc=Decimal(50)))
    assert filt.apply([_opp(size="1000", conservative="0")]) == []
    assert filt.stats.below_notional == 1


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "min_profit_bps": Decimal(30),
        "min_notional_usdc": Decimal(50),
        "dedupe_cooldown_seconds": 300.0,
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def test_rejects_below_profit_threshold() -> None:
    filt = OpportunityFilter(_settings(min_profit_bps=Decimal(500)))
    assert filt.apply([_opp(bps="200")]) == []
    assert filt.stats.below_profit == 1


def test_rejects_below_notional() -> None:
    # size 10 * cost 0.90 = 9 USDC < 50
    filt = OpportunityFilter(_settings())
    assert filt.apply([_opp(size="10")]) == []
    assert filt.stats.below_notional == 1


def test_annualized_gate_rejects_low_return_held_arb() -> None:
    # 8% floor; a held arb returning 1.5%/yr (like the OpenAI basket) is dropped, size irrelevant.
    filt = OpportunityFilter(_settings(min_annualized_return=Decimal("0.08")))
    assert filt.apply([_opp(size="1000", annualized="0.015")]) == []
    assert filt.stats.below_annualized == 1


def test_annualized_gate_allows_high_return_held_arb() -> None:
    filt = OpportunityFilter(_settings(min_annualized_return=Decimal("0.08")))
    assert len(filt.apply([_opp(size="1000", annualized="0.20")])) == 1


def test_annualized_gate_exempts_instant_arbs() -> None:
    # Instant arbs have no lockup (annualized None) → never gated, even with a high floor.
    filt = OpportunityFilter(_settings(min_annualized_return=Decimal("0.50")))
    assert len(filt.apply([_opp(size="1000", annualized=None)])) == 1


def test_annualized_gate_disabled_by_default() -> None:
    # Default 0 → the gate is a no-op; a 1.5%/yr held arb passes.
    filt = OpportunityFilter(_settings())
    assert len(filt.apply([_opp(size="1000", annualized="0.015")])) == 1
    assert filt.stats.below_annualized == 0


# --- executability gate (rec #2 / D5): per-leg minimum order size ---
# Tested at a lowered $1 floor — at the $50 floor the min-order gate is a redundant no-op
# (clearing $50 already needs »5 shares); its job is to keep a LOWERED floor honest.

_LEGS = [
    Leg(token_id="t1", side="buy", price=Decimal("0.45"), size=Decimal("100")),
    Leg(token_id="t2", side="buy", price=Decimal("0.45"), size=Decimal("100")),
]
_MIN5 = {"t1": Decimal(5), "t2": Decimal(5)}


def test_min_order_gate_rejects_sub_minimum_basket() -> None:
    # decision_size 4 (< 5-share min) at a $1 floor: notional $3.60 clears $1, but the basket
    # can't be placed → rejected as unexecutable, not surfaced as a phantom edge.
    filt = OpportunityFilter(_settings(min_notional_usdc=Decimal(1)))
    opp = _opp(size="1000", conservative="4", cost="0.90", legs=_LEGS)
    assert filt.apply([opp], _MIN5) == []
    assert filt.stats.below_min_order == 1


def test_min_order_gate_allows_placeable_basket() -> None:
    filt = OpportunityFilter(_settings(min_notional_usdc=Decimal(1)))
    opp = _opp(size="1000", conservative="10", cost="0.90", legs=_LEGS)
    assert len(filt.apply([opp], _MIN5)) == 1


def test_min_order_gate_skips_when_minimum_unknown() -> None:
    # No min-size context (or token absent) → gate can't fire; opp passes on other filters.
    filt = OpportunityFilter(_settings(min_notional_usdc=Decimal(1)))
    opp = _opp(size="1000", conservative="4", cost="0.90", legs=_LEGS)
    assert len(filt.apply([opp], None)) == 1


def test_min_order_gate_disabled_by_config() -> None:
    filt = OpportunityFilter(_settings(min_notional_usdc=Decimal(1), enforce_min_order_size=False))
    opp = _opp(size="1000", conservative="4", cost="0.90", legs=_LEGS)
    assert len(filt.apply([opp], _MIN5)) == 1


def test_rejects_at_risk_when_configured() -> None:
    filt = OpportunityFilter(_settings(exclude_at_risk_resolution=True))
    assert filt.apply([_opp(risk=ResolutionRisk.AT_RISK)]) == []
    assert filt.stats.at_risk == 1


def test_allows_at_risk_when_not_excluding() -> None:
    filt = OpportunityFilter(_settings(exclude_at_risk_resolution=False))
    assert len(filt.apply([_opp(risk=ResolutionRisk.AT_RISK)])) == 1


def test_passes_good_opportunity() -> None:
    filt = OpportunityFilter(_settings())
    assert len(filt.apply([_opp()])) == 1
    assert filt.stats.kept == 1  # passed all filters (not the store/notify success count)


def test_dedupe_suppresses_repeat_within_cooldown() -> None:
    clock = {"t": 1000.0}
    cache = DedupeCache(cooldown_seconds=300.0, now=lambda: clock["t"])
    filt = OpportunityFilter(_settings(), cache)
    assert len(filt.apply([_opp()])) == 1
    assert filt.apply([_opp()]) == []  # same key, still in cooldown
    clock["t"] += 301
    assert len(filt.apply([_opp()])) == 1  # cooldown elapsed


def test_dedupe_key_distinguishes_detector_and_markets() -> None:
    a = _opp(conditions=["0xA"])
    b = _opp(conditions=["0xB"])
    assert opportunity_key(a) != opportunity_key(b)
