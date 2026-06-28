"""NegRisk basket arbitrage — across a multi-outcome event, Σ YES prices ≠ 1.

For an event with mutually-exclusive, exhaustive outcomes ``o_1..o_N`` (exactly one resolves
YES), if ``Σ_i a_yes,i < 1`` you can buy 1 YES of every outcome through the **standard order
books**: exactly one pays 1 at resolution.

    net_profit = 1 - Σ_i a_yes,i - f - g      (paid at resolution)
    annualized = (net_profit / cost) * (365 / days_to_resolution)

⚠️ The NegRisk **convert** function is NOT the tool for this. Convert is a capital-efficiency
mechanism: you pay 1 unit and receive 1 unit of exposure — **zero profit** (see
:func:`negrisk_convert_pnl`). Using convert to "capture" an underpriced sum just locks
collateral. The edge comes from buying the underpriced basket on the books, not from convert.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import ClassVar

from polyarb.detectors.base import ZERO, Profit, Snapshot, make_opportunity
from polyarb.models import DetectorKind, Leg, Opportunity, OrderBook
from polyarb.pricing.fees import fee_rate_for, taker_fee
from polyarb.pricing.sizing import depth_at_or_better, executable_size

ONE = Decimal(1)


def basket_profit(asks: list[Decimal], fee_rates: list[Decimal], gas: Decimal) -> Profit:
    """Profit from buying 1 YES of every outcome at ``asks``. Cost = Σ asks; payoff = 1."""
    cost = sum(asks, ZERO)
    gross = ONE - cost
    fees = sum((taker_fee(p, ONE, r) for p, r in zip(asks, fee_rates, strict=True)), ZERO)
    return Profit(cost=cost, gross_profit=gross, fees=fees, gas=gas)


def negrisk_convert_pnl(prices: list[Decimal]) -> Decimal:
    """The P&L of the NegRisk *convert* operation: always exactly 0.

    Convert exchanges 1 unit of collateral for 1 unit of exposure regardless of prices. It is
    a capital-efficiency tool, never a source of arbitrage profit. Encoded as a function so
    the invariant is property-tested (see tests). Do NOT route basket arbs through convert.
    """
    return ZERO


class NegRiskBasketDetector:
    kind: ClassVar[DetectorKind] = DetectorKind.NEGRISK_BASKET

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        event = snap.event
        if event is None or not event.is_multi_outcome:
            return

        legs: list[Leg] = []
        asks: list[Decimal] = []
        fee_rates: list[Decimal] = []
        depths: list[Decimal] = []
        condition_ids: list[str] = []

        for market in event.markets:
            if not market.is_binary:
                return  # not a clean YES/NO basket; skip the whole event
            book: OrderBook | None = snap.books.get(market.yes_token_id)
            ask = book.best_ask if book is not None else None
            if book is None or ask is None:
                return  # cannot lock the basket without every leg's YES ask
            asks.append(ask.price)
            fee_rates.append(fee_rate_for(market))
            depths.append(depth_at_or_better(book, "buy", ask.price))
            condition_ids.append(market.condition_id)
            legs.append(
                Leg(
                    token_id=market.yes_token_id,
                    side="buy",
                    price=ask.price,
                    size=ONE,
                    outcome=market.group_item_title or market.outcomes[0],
                )
            )

        profit = basket_profit(asks, fee_rates, snap.gas)
        if profit.net_profit <= ZERO:
            return

        days = next(
            (
                snap.days_to_resolution[m.condition_id]
                for m in event.markets
                if m.condition_id in snap.days_to_resolution
            ),
            None,
        )
        yield make_opportunity(
            detector=self.kind,
            description=f"negrisk basket under: {event.title.strip()} (Σ YES = {sum(asks, ZERO)})",
            condition_ids=condition_ids,
            legs=legs,
            profit=profit,
            executable_size=executable_size(depths),
            realizes="resolution",
            event_id=event.id,
            days_to_resolution=days,
        )
