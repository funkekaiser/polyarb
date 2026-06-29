"""Typed domain models for Polymarket data.

Field shapes verified 2026-06-28 against recorded fixtures (see tests/fixtures/ and
docs/API_NOTES.md). Notable real-API quirks these models normalize:

- Gamma encodes ``outcomes``, ``outcomePrices`` and ``clobTokenIds`` as JSON-*strings*,
  not arrays — we parse them. ``clobTokenIds[i]`` lines up with ``outcomes[i]`` (index 0 =
  "Yes", index 1 = "No" for binary markets).
- The CLOB order book returns ``bids`` ascending and ``asks`` descending (worst→best), with
  prices/sizes as strings. We do NOT trust that ordering: ``best_bid``/``best_ask`` are
  computed by value (max bid, min ask) so the arb math is correct regardless.
- Prices are modeled as ``Decimal`` to avoid float drift in the profit identities.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


def _parse_json_list(value: Any) -> Any:
    """Gamma sends list-typed fields as JSON-encoded strings; decode them."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _blank_to_none(value: Any) -> Any:
    """Live API sends "" for some unset numeric fields; treat blank as missing."""
    return None if value == "" else value


class Market(BaseModel):
    """A single Polymarket market (one binary Yes/No question)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    condition_id: str = Field(alias="conditionId")
    question: str
    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[Decimal] = Field(default_factory=list, alias="outcomePrices")
    # Some discovered markets aren't tradeable yet and omit clobTokenIds entirely; default
    # to empty so event discovery doesn't crash. Token access is gated behind ``is_binary``.
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    neg_risk: bool = Field(default=False, alias="negRisk")
    neg_risk_market_id: str | None = Field(default=None, alias="negRiskMarketID")
    # Marks the "Other / none-of-the-above" catch-all leg of a negRisk group. Informational
    # only — surfaced for labeling/diagnostics and future use (e.g. the NO-dual). The basket's
    # exhaustiveness guarantee rests on the event being non-augmented (exhaustive by
    # construction), not on this flag; a present Other market is included as an ordinary leg and
    # a missing Other can't be distinguished from a genuinely exhaustive set. Was being dropped
    # silently before (extra="ignore").
    neg_risk_other: bool = Field(default=False, alias="negRiskOther")
    group_item_title: str | None = Field(default=None, alias="groupItemTitle")
    fees_enabled: bool = Field(default=False, alias="feesEnabled")
    fee_type: str | None = Field(default=None, alias="feeType")
    fee_rate: Decimal | None = None
    tick_size: Decimal | None = Field(default=None, alias="orderPriceMinTickSize")
    min_order_size: Decimal | None = Field(default=None, alias="orderMinSize")
    best_bid: Decimal | None = Field(default=None, alias="bestBid")
    best_ask: Decimal | None = Field(default=None, alias="bestAsk")
    accepting_orders: bool = Field(default=True, alias="acceptingOrders")
    active: bool = True
    closed: bool = False
    end_date: datetime | None = Field(default=None, alias="endDate")  # market resolution time
    # UMA dispute-window length (seconds); 0 = use the protocol default. A *longer-than-default*
    # value is a weak signal of a more contention-prone resolution (A2 — see
    # resolution/risk.classify_market). Not a void probability. Was dropped before (extra=ignore).
    custom_liveness: int = Field(default=0, alias="customLiveness")

    @field_validator("custom_liveness", mode="before")
    @classmethod
    def _liveness_default(cls, value: Any) -> Any:
        return 0 if value in (None, "") else value

    @field_validator("outcomes", "outcome_prices", "clob_token_ids", mode="before")
    @classmethod
    def _decode_json_lists(cls, value: Any) -> Any:
        return _parse_json_list(value)

    @field_validator(
        "best_bid", "best_ask", "fee_rate", "tick_size", "min_order_size", "end_date", mode="before"
    )
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @model_validator(mode="before")
    @classmethod
    def _flatten_fee_schedule(cls, data: Any) -> Any:
        # feeSchedule is {"rate": 0.07, ...} on fee'd markets, null on fee-free ones.
        if isinstance(data, dict) and data.get("fee_rate") is None:
            schedule = data.get("feeSchedule")
            if isinstance(schedule, dict) and "rate" in schedule:
                data = {**data, "fee_rate": schedule["rate"]}
        return data

    @property
    def is_binary(self) -> bool:
        return len(self.clob_token_ids) == 2

    @property
    def yes_token_id(self) -> str:
        return self.clob_token_ids[0]

    @property
    def no_token_id(self) -> str:
        return self.clob_token_ids[1]

    @property
    def is_fee_free(self) -> bool:
        """True when no taker fee applies (e.g. geopolitics/world events)."""
        return not self.fees_enabled or self.fee_type is None or self.fee_rate in (None, Decimal(0))

    def yes_outcome(self) -> Outcome:
        """The YES side as a standalone Outcome (used by the NegRisk basket detector)."""
        if not self.is_binary:
            raise ValueError(f"yes_outcome() requires a binary market; {self.condition_id} is not")
        name = self.group_item_title or (self.outcomes[0] if self.outcomes else "Yes")
        return Outcome(
            name=name,
            token_id=self.yes_token_id,
            condition_id=self.condition_id,
            price=self.outcome_prices[0] if self.outcome_prices else None,
        )


class Outcome(BaseModel):
    """One selectable outcome — a market's YES side, used for multi-outcome baskets."""

    name: str
    token_id: str
    condition_id: str
    price: Decimal | None = None


class Event(BaseModel):
    """A Polymarket event grouping one or more markets (multi-outcome when negRisk)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    ticker: str | None = None
    title: str
    active: bool = True
    closed: bool = False
    neg_risk: bool = Field(default=False, alias="negRisk")
    neg_risk_market_id: str | None = Field(default=None, alias="negRiskMarketID")
    enable_neg_risk: bool = Field(default=False, alias="enableNegRisk")
    neg_risk_augmented: bool = Field(default=False, alias="negRiskAugmented")
    markets: list[Market] = Field(default_factory=list)

    @property
    def is_multi_outcome(self) -> bool:
        """A negRisk event with N>=3 mutually-exclusive markets (NegRisk basket candidate)."""
        return self.neg_risk and len(self.markets) >= 3

    def outcomes(self) -> list[Outcome]:
        """YES outcomes of the binary constituent markets (non-binary markets are skipped)."""
        return [m.yes_outcome() for m in self.markets if m.is_binary]


class BookLevel(BaseModel):
    """One price level in an order book. Prices and sizes are Decimals."""

    price: Decimal
    size: Decimal


class OrderBook(BaseModel):
    """A CLOB order book snapshot for a single token (outcome side)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    market: str  # conditionId
    asset_id: str  # token_id
    timestamp_ms: int = Field(alias="timestamp")
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    neg_risk: bool = False
    last_trade_price: Decimal | None = None

    @field_validator("timestamp_ms", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> Any:
        # Go via float so a fractional string ("1700000000.9") doesn't raise — int("..9")
        # would. Plain int strings and JSON floats both round-trip correctly.
        return int(float(value)) if isinstance(value, str | float) else value

    @field_validator("tick_size", "min_order_size", "last_trade_price", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @property
    def best_bid(self) -> BookLevel | None:
        """Highest-priced bid with real size (computed by value, not list position).

        Zero-size levels are skipped: a phantom 0-size level at a better price would
        otherwise become the "best" quote, yielding an opportunity with executable_size 0
        while masking the real, fillable level just behind it.
        """
        return max(
            (level for level in self.bids if level.size > 0),
            key=lambda level: level.price,
            default=None,
        )

    @property
    def best_ask(self) -> BookLevel | None:
        """Lowest-priced ask with real size (computed by value, not list position).

        Zero-size levels are skipped (see :meth:`best_bid`).
        """
        return min(
            (level for level in self.asks if level.size > 0),
            key=lambda level: level.price,
            default=None,
        )


class DetectorKind(StrEnum):
    COMPLEMENT = "complement"
    NEGRISK_BASKET = "negrisk_basket"
    NEGRISK_DUAL = "negrisk_dual"  # buy 1 NO of every outcome (Σ NO < M-1)
    PARTIAL_BASKET = "partial_basket"  # §5 — opt-in directional (NOT structural) partial basket
    DEPENDENCY = "dependency"


class Leg(BaseModel):
    """One executable leg of an arbitrage (buy or sell a token at a price for a size)."""

    token_id: str
    side: Literal["buy", "sell"]
    price: Decimal
    size: Decimal
    outcome: str | None = None


class Opportunity(BaseModel):
    """A detected structural arb, with per-set cost/profit fields and execution-level totals.

    ``cost``, ``gross_profit``, ``fees``, and ``net_profit`` are *per set* (one unit of the
    locked position). ``gas`` is a fixed per-execution cost (one merge/split/settlement tx,
    regardless of how many sets are traded). ``total_net_profit`` and ``net_profit_bps`` are
    gas-adjusted execution-level figures: they reflect the economics of the whole trade.
    """

    detector: DetectorKind
    description: str
    event_id: str | None = None
    condition_ids: list[str] = Field(default_factory=list)
    legs: list[Leg] = Field(default_factory=list)
    cost: Decimal  # capital deployed per set
    gross_profit: Decimal  # before fees, per set
    fees: Decimal  # total taker fees, per set
    gas: Decimal  # gas estimate, per EXECUTION (fixed cost, not per set)
    net_profit: Decimal  # gross - fees, per set (before gas)
    net_profit_bps: Decimal  # gas-adjusted net return on deployed capital, in bps
    executable_size: Decimal  # sets supported by book depth
    realizes: Literal["instant", "resolution"]
    days_to_resolution: int | None = None
    annualized: Decimal | None = None
    resolution_risk: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_net_profit(self) -> Decimal:
        """Net dollars after the per-execution gas cost, across executable_size."""
        return self.executable_size * self.net_profit - self.gas
