"""Synthetic model builders for detector/pricing tests (no network)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from polyarb.models import BookLevel, Event, Market, OrderBook

Levels = Sequence[tuple[str, str]]  # (price, size) as strings


def make_book(
    token_id: str = "t",
    *,
    bids: Levels = (),
    asks: Levels = (),
    neg_risk: bool = False,
) -> OrderBook:
    return OrderBook(
        market="0xcond",
        asset_id=token_id,
        timestamp_ms=1,
        bids=[BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids],
        asks=[BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks],
        neg_risk=neg_risk,
    )


def make_market(
    condition_id: str = "0xA",
    *,
    yes: str = "y",
    no: str = "n",
    fee_rate: float | None = None,
    fee_type: str | None = None,
    group_item_title: str | None = None,
    neg_risk: bool = False,
) -> Market:
    # fee_type drives resolution-risk classification (crypto/sports/price → OBJECTIVE); pass it
    # explicitly to get an OBJECTIVE market WITHOUT fees. Default: derive from fee_rate as before.
    resolved_fee_type = fee_type or ("crypto_fees_v2" if fee_rate is not None else None)
    return Market(
        id="1",
        condition_id=condition_id,
        question="Q?",
        outcomes=["Yes", "No"],
        clob_token_ids=[yes, no],
        neg_risk=neg_risk,
        fees_enabled=fee_rate is not None,
        fee_type=resolved_fee_type,
        fee_rate=Decimal(str(fee_rate)) if fee_rate is not None else None,
        group_item_title=group_item_title,
    )


def make_event(markets: list[Market], *, title: str = "Evt", neg_risk: bool = True) -> Event:
    return Event(
        id="9",
        title=title,
        neg_risk=neg_risk,
        enable_neg_risk=neg_risk,
        markets=markets,
    )
