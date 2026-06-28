"""Resolution-risk classification tests."""

from __future__ import annotations

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
