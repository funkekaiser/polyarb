"""Resolution-risk classification tests."""

from __future__ import annotations

from decimal import Decimal

from polyarb.models import Market
from polyarb.resolution.risk import (
    ResolutionRisk,
    aggregate_risk,
    classify_market,
    risk_rank,
)
from tests.helpers import make_market


def test_crypto_and_sports_are_objective() -> None:
    assert (
        classify_market(make_market(fee_rate=0.07)) == ResolutionRisk.OBJECTIVE
    )  # crypto fee type


def test_fee_free_is_standard() -> None:
    assert classify_market(make_market(fee_rate=None)) == ResolutionRisk.STANDARD


def test_aggregate_takes_worst() -> None:
    objective = make_market("0x1", fee_rate=0.07)
    standard = make_market("0x2", fee_rate=None)
    assert aggregate_risk([objective, standard]) == ResolutionRisk.STANDARD


def test_aggregate_empty_defaults_standard() -> None:
    assert aggregate_risk([]) == ResolutionRisk.STANDARD


def test_risk_rank_orders_objective_below_at_risk() -> None:
    assert risk_rank(ResolutionRisk.OBJECTIVE) < risk_rank(ResolutionRisk.AT_RISK)
    assert risk_rank("nonsense") == risk_rank(ResolutionRisk.STANDARD)


def test_directional_ranks_below_structural_above_at_risk() -> None:
    # §5: a directional partial basket must never outrank a structural arb, but must survive the
    # default exclude_at_risk filter (an explicit opt-in): ELEVATED < DIRECTIONAL < AT_RISK.
    assert risk_rank(ResolutionRisk.ELEVATED) < risk_rank(ResolutionRisk.DIRECTIONAL)
    assert risk_rank(ResolutionRisk.DIRECTIONAL) < risk_rank(ResolutionRisk.AT_RISK)


def _market_with_liveness(seconds: int) -> Market:
    return Market(
        id="1",
        condition_id="0xA",
        question="Q?",
        outcomes=["Yes", "No"],
        clob_token_ids=["y", "n"],
        fees_enabled=True,
        fee_type="crypto_fees_v2",  # would classify OBJECTIVE on category alone
        fee_rate=Decimal("0.07"),
        custom_liveness=seconds,
    )


def _market_with_uma(statuses: list[str]) -> Market:
    return Market(
        id="1",
        condition_id="0xA",
        question="Q?",
        outcomes=["Yes", "No"],
        clob_token_ids=["y", "n"],
        fees_enabled=True,
        fee_type="crypto_fees_v2",  # OBJECTIVE on category alone
        fee_rate=Decimal("0.07"),
        uma_resolution_statuses=statuses,
    )


def test_active_dispute_is_at_risk() -> None:
    # C1: an active UMA dispute → AT_RISK, overriding the otherwise-OBJECTIVE category, so the
    # default exclude_at_risk filter drops the (contested, not-actually-guaranteed) arb.
    assert classify_market(_market_with_uma(["proposed", "disputed"])) == ResolutionRisk.AT_RISK


def test_proposed_without_dispute_keeps_category() -> None:
    # "proposed" is the normal resolution flow, NOT a dispute → category stands (no over-exclusion).
    assert classify_market(_market_with_uma(["proposed"])) == ResolutionRisk.OBJECTIVE
    assert classify_market(_market_with_uma([])) == ResolutionRisk.OBJECTIVE


def test_long_liveness_is_elevated() -> None:
    # A2: a longer-than-default UMA dispute window is a weak contention signal → ELEVATED
    # (rank-down, NOT hard-exclude), overriding the otherwise-OBJECTIVE crypto category.
    assert classify_market(_market_with_liveness(10800)) == ResolutionRisk.ELEVATED  # 3h > 2h


def test_short_or_default_liveness_keeps_category() -> None:
    # Default (0) and shorter-than-default windows do NOT elevate — would fail if the threshold
    # were `> 0` instead of `> _UMA_DEFAULT_LIVENESS_S`.
    assert classify_market(_market_with_liveness(0)) == ResolutionRisk.OBJECTIVE
    assert classify_market(_market_with_liveness(600)) == ResolutionRisk.OBJECTIVE  # 10m < 2h
    assert classify_market(make_market(fee_rate=0.07)) == ResolutionRisk.OBJECTIVE
