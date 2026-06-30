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


# ---------------------------------------------------------------------------
# D2 — yes_index: YES/NO token resolution driven by outcome labels
# ---------------------------------------------------------------------------


def _make_binary(
    outcomes: list[str],
    prices: list[str] | None = None,
    token_ids: list[str] | None = None,
) -> Market:
    """Build a binary Market the same way Gamma sends it: list fields as JSON-strings."""
    if prices is None:
        prices = ["0.6", "0.4"]
    if token_ids is None:
        token_ids = ["tok_yes", "tok_no"]
    return Market.model_validate(
        {
            "id": "1",
            "conditionId": "0x1",
            "question": "q",
            "outcomes": json.dumps(outcomes),
            "outcomePrices": json.dumps(prices),
            "clobTokenIds": json.dumps(token_ids),
        }
    )


def test_yes_index_canonical() -> None:
    """D2: canonical ['Yes', 'No'] → yes_index==0, token/price at index 0."""
    m = _make_binary(["Yes", "No"])
    assert m.yes_index == 0
    assert m.yes_token_id == m.clob_token_ids[0]
    assert m.no_token_id == m.clob_token_ids[1]


def test_yes_index_reversed_pair() -> None:
    """D2: reversed ['No', 'Yes'] → yes_index==1, YES token/price are at index 1."""
    m = _make_binary(["No", "Yes"], prices=["0.4", "0.6"], token_ids=["tok_no", "tok_yes"])
    assert m.yes_index == 1
    assert m.yes_token_id == "tok_yes"  # clob_token_ids[1]
    assert m.no_token_id == "tok_no"  # clob_token_ids[0]
    # yes_outcome() must track the YES side, not blindly use index 0
    outcome = m.yes_outcome()
    assert outcome.token_id == "tok_yes"
    assert outcome.price == Decimal("0.6")


def test_yes_index_case_and_whitespace_variants() -> None:
    """D2: detection is case-insensitive and strips whitespace."""
    # Upper-case YES at index 0
    m_upper = _make_binary(["YES", " no "])
    assert m_upper.yes_index == 0

    # Lower-case yes at index 1 (reversed)
    m_lower_rev = _make_binary(["no", "yes"], token_ids=["tok_no", "tok_yes"])
    assert m_lower_rev.yes_index == 1
    assert m_lower_rev.yes_token_id == "tok_yes"

    # Mixed case reversed
    m_mixed = _make_binary([" No ", " Yes "], token_ids=["tok_no", "tok_yes"])
    assert m_mixed.yes_index == 1
    assert m_mixed.yes_token_id == "tok_yes"


def test_yes_index_empty_outcomes_fallback() -> None:
    """D2: empty outcomes → yes_index==0 (documented fallback), token access unchanged."""
    m = Market.model_validate(
        {
            "id": "1",
            "conditionId": "0x1",
            "question": "q",
            "outcomes": "[]",
            "clobTokenIds": '["tok_yes", "tok_no"]',
        }
    )
    assert m.yes_index == 0
    assert m.yes_token_id == "tok_yes"
    assert m.no_token_id == "tok_no"


def test_yes_index_non_yes_no_pair_fallback() -> None:
    """D2: non-Yes/No labels like ['Trump', 'Biden'] → yes_index==0 (documented fallback)."""
    m = _make_binary(["Trump", "Biden"], token_ids=["tok_trump", "tok_biden"])
    assert m.yes_index == 0
    assert m.yes_token_id == "tok_trump"  # index 0 — can't do better without labels
