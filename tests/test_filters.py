"""Emission filter + dedupe tests."""

from __future__ import annotations

from decimal import Decimal

from polyarb.config import Settings
from polyarb.engine.filters import DedupeCache, OpportunityFilter, opportunity_key
from polyarb.models import DetectorKind, Opportunity
from polyarb.resolution.risk import ResolutionRisk


def _opp(
    *,
    bps: str = "200",
    cost: str = "0.90",
    size: str = "100",
    risk: ResolutionRisk = ResolutionRisk.OBJECTIVE,
    conditions: list[str] | None = None,
) -> Opportunity:
    return Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description="t",
        condition_ids=conditions or ["0x1"],
        legs=[],
        cost=Decimal(cost),
        gross_profit=Decimal("0.10"),
        fees=Decimal(0),
        gas=Decimal(0),
        net_profit=Decimal("0.10"),
        net_profit_bps=Decimal(bps),
        executable_size=Decimal(size),
        realizes="instant",
        resolution_risk=risk,
    )


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
