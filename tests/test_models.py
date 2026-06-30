"""Parse recorded fixtures into domain models (offline). Verifies the real-API quirks."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from polyarb.models import Event, Market, OrderBook

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_binary_market_parses_and_is_fee_free() -> None:
    m = Market.model_validate(load("gamma_binary_market.json"))
    assert m.is_binary
    assert not m.neg_risk
    # clobTokenIds arrives as a JSON-encoded string; it must decode to two ids.
    assert len(m.clob_token_ids) == 2
    assert m.yes_token_id == m.clob_token_ids[0]
    assert m.no_token_id == m.clob_token_ids[1]
    assert m.outcomes == ["Yes", "No"]
    assert m.is_fee_free
    assert m.fee_rate is None


def test_feed_market_extracts_fee_rate_from_schedule() -> None:
    m = Market.model_validate(load("gamma_feed_market.json"))
    assert m.fees_enabled
    assert m.fee_type == "crypto_fees_v2"
    assert m.fee_rate == Decimal("0.07")
    assert not m.is_fee_free
    assert m.custom_liveness == 600  # A2: real fixture carries customLiveness; must parse


def test_negrisk_event_is_multi_outcome() -> None:
    e = Event.model_validate(load("gamma_negrisk_event.json"))
    assert e.neg_risk
    assert e.enable_neg_risk
    assert e.is_multi_outcome
    assert len(e.markets) >= 3
    outcomes = e.outcomes()
    assert len(outcomes) == len(e.markets)
    # Each outcome maps to a distinct YES token id.
    assert len({o.token_id for o in outcomes}) == len(outcomes)


def test_market_parses_end_date_and_blank() -> None:
    from datetime import datetime

    m = Market.model_validate(load("gamma_binary_market.json"))
    assert isinstance(m.end_date, datetime)
    # blank end date coerces to None rather than failing to parse
    blank = Market.model_validate(
        {
            "id": "1",
            "conditionId": "0x1",
            "question": "q",
            "clobTokenIds": '["a","b"]',
            "endDate": "",
        }
    )
    assert blank.end_date is None


def test_order_book_best_levels_computed_by_value() -> None:
    b = OrderBook.model_validate(load("clob_book_binary.json"))
    assert isinstance(b.timestamp_ms, int)
    assert b.best_bid is not None
    assert b.best_ask is not None
    # Best bid/ask are the max bid and min ask regardless of list ordering.
    assert b.best_bid.price == max(level.price for level in b.bids)
    assert b.best_ask.price == min(level.price for level in b.asks)
    assert b.best_ask.price > b.best_bid.price
    assert all(isinstance(level.price, Decimal) for level in b.bids)


def test_market_neg_risk_other_field_parsed() -> None:
    """F: negRiskOther alias round-trips; defaults to False when absent.

    The field was previously swallowed by extra='ignore' and is now load-bearing for
    exhaustiveness checking in the NegRisk basket detector.
    """
    with_flag = Market.model_validate(
        {"id": "1", "conditionId": "0x1", "question": "q", "negRiskOther": True}
    )
    assert with_flag.neg_risk_other is True

    without_flag = Market.model_validate({"id": "1", "conditionId": "0x1", "question": "q"})
    assert without_flag.neg_risk_other is False


def test_market_custom_liveness_parsed() -> None:
    """A2: customLiveness alias round-trips; absent or null/blank coerces to 0 (was dropped)."""
    base = {"id": "1", "conditionId": "0x1", "question": "q"}
    assert Market.model_validate({**base, "customLiveness": 120}).custom_liveness == 120
    assert Market.model_validate({**base, "customLiveness": "120"}).custom_liveness == 120
    assert Market.model_validate(base).custom_liveness == 0  # absent → 0
    assert Market.model_validate({**base, "customLiveness": None}).custom_liveness == 0  # null → 0
    assert Market.model_validate({**base, "customLiveness": ""}).custom_liveness == 0  # blank → 0


def test_market_uma_resolution_statuses_parsed() -> None:
    """C1: umaResolutionStatuses (JSON list-string) round-trips; null/blank/absent coerce to []."""
    base = {"id": "1", "conditionId": "0x1", "question": "q"}

    def uma(payload: dict) -> list[str]:
        return Market.model_validate({**base, **payload}).uma_resolution_statuses

    assert uma({"umaResolutionStatuses": '["proposed", "disputed"]'}) == ["proposed", "disputed"]
    assert uma({"umaResolutionStatuses": "[]"}) == []
    assert uma({}) == []  # absent
    assert uma({"umaResolutionStatuses": None}) == []  # null


def test_market_yes_index_canonical_is_zero() -> None:
    """D2-residual: canonical outcomes ["Yes","No"] → yes_index=0."""
    m = Market.model_validate(
        {"id": "1", "conditionId": "0x1", "question": "q", "outcomes": '["Yes","No"]'}
    )
    assert m.yes_index == 0


def test_market_yes_index_reversed_is_one() -> None:
    """D2-residual: reversed outcomes ["No","Yes"] → yes_index=1."""
    m = Market.model_validate(
        {"id": "1", "conditionId": "0x1", "question": "q", "outcomes": '["No","Yes"]'}
    )
    assert m.yes_index == 1


def test_market_yes_index_empty_or_other_falls_back_to_zero() -> None:
    """D2-residual: empty and non-Yes-No outcome lists → yes_index=0 (safe fallback)."""
    base = {"id": "1", "conditionId": "0x1", "question": "q"}
    assert Market.model_validate(base).yes_index == 0  # empty outcomes → 0
    m_other = Market.model_validate({**base, "outcomes": '["Maybe","Dunno"]'})
    assert m_other.yes_index == 0  # non-Yes-No → 0


def test_opportunity_live_count_total_count_default_none() -> None:
    """A1-riskwt: live_count/total_count default None; existing Opportunity construction ok."""
    from decimal import Decimal

    from polyarb.models import DetectorKind, Opportunity

    opp = Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description="test",
        condition_ids=["0x1"],
        legs=[],
        cost=Decimal("0.90"),
        gross_profit=Decimal("0.10"),
        fees=Decimal(0),
        gas=Decimal(0),
        net_profit=Decimal("0.10"),
        net_profit_bps=Decimal("100"),
        executable_size=Decimal(100),
        realizes="resolution",
    )
    assert opp.live_count is None
    assert opp.total_count is None


def test_opportunity_live_count_total_count_set() -> None:
    """A1-riskwt: live_count/total_count are stored and retrieved correctly."""
    from decimal import Decimal

    from polyarb.models import DetectorKind, Opportunity

    opp = Opportunity(
        detector=DetectorKind.NEGRISK_BASKET,
        description="test",
        condition_ids=["0x1"],
        legs=[],
        cost=Decimal("0.90"),
        gross_profit=Decimal("0.10"),
        fees=Decimal(0),
        gas=Decimal(0),
        net_profit=Decimal("0.10"),
        net_profit_bps=Decimal("100"),
        executable_size=Decimal(100),
        realizes="resolution",
        live_count=3,
        total_count=5,
    )
    assert opp.live_count == 3
    assert opp.total_count == 5
