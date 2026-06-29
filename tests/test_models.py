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
