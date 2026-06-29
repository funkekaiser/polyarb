"""NegRisk basket arbitrage — across a multi-outcome event, Σ YES prices ≠ 1.

For an event with mutually-exclusive, exhaustive outcomes ``o_1..o_N`` (exactly one resolves
YES), if ``Σ_i a_yes,i < 1`` you can buy 1 YES of every outcome through the **standard order
books**: exactly one pays 1 at resolution.

    net_profit (per set) = 1 - Σ_i a_yes,i - f
    total_net_profit     = size * net_profit - gas   (gas applied once at execution level)
    annualized           = (total_net / total_cost) * (365 / days_to_resolution)

⚠️ The NegRisk **convert** function is NOT the tool for this. Convert is a capital-efficiency
mechanism: you pay 1 unit and receive 1 unit of exposure — **zero profit** (see
:func:`negrisk_convert_pnl`). Using convert to "capture" an underpriced sum just locks
collateral. The edge comes from buying the underpriced basket on the books, not from convert.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import ClassVar

from polyarb.detectors.base import (
    ONE,
    ZERO,
    Profit,
    Snapshot,
    make_opportunity,
    walk_and_size_buy_basket,
)
from polyarb.models import BookLevel, DetectorKind, Event, Leg, Market, Opportunity, OrderBook
from polyarb.pricing.fees import fee_rate_for, taker_fee
from polyarb.pricing.sizing import is_crossed

# A closed constituent's resolved YES price tells us how it resolved: ~0 means it LOST
# (eliminated — safe to drop from the partition). Anything else — ~1 (it WON, so the event is
# decided and the live set excludes the winner), ~0.5 (void), or unknown/missing — means
# dropping it is unsafe, so we skip the whole event rather than build a basket of losers.
_RESOLVED_NO_MAX = Decimal("0.02")


def basket_profit(asks: list[Decimal], fee_rates: list[Decimal]) -> Profit:
    """Profit from buying 1 YES of every outcome at ``asks``. Cost = Σ asks; payoff = 1."""
    cost = sum(asks, ZERO)
    gross = ONE - cost
    fees = sum((taker_fee(p, ONE, r) for p, r in zip(asks, fee_rates, strict=True)), ZERO)
    return Profit(cost=cost, gross_profit=gross, fees=fees)


def negrisk_convert_pnl(prices: list[Decimal]) -> Decimal:
    """The P&L of the NegRisk *convert* operation: always exactly 0.

    Convert exchanges 1 unit of collateral for 1 unit of exposure regardless of prices. It is
    a capital-efficiency tool, never a source of arbitrage profit. Encoded as a function so
    the invariant is property-tested (see tests). Do NOT route basket arbs through convert.
    """
    return ZERO


def live_partition(event: Event, *, skip_augmented: bool = True) -> list[Market] | None:
    """The live (non-eliminated) constituents of a negRisk event, or None if not a safe basket.

    A1 exhaustiveness, factored out so the YES basket and the (future) NO-dual share it. Drops a
    ``closed`` market only when its resolved YES price proves it LOST (~0); a closed winner (~1),
    void (~0.5), or unknown price means we can't safely drop it (the winner closes first while
    losers' books go stale-cheap) → return None and skip the event. Also returns None on a
    partition hole (a live market that isn't binary/active/accepting_orders), a decided/inactive
    event, fewer than two live outcomes, or — when ``skip_augmented`` — an augmented event (the
    YES basket needs full exhaustiveness; the NO-dual needs only mutual exclusivity, so it passes
    ``skip_augmented=False``). Does NOT check book availability — callers validate each leg's book.
    """
    if skip_augmented and event.neg_risk_augmented:
        return None
    if event.closed or not event.active:
        return None
    live: list[Market] = []
    for market in event.markets:
        if market.closed:
            yes_price = market.outcome_prices[0] if market.outcome_prices else None
            if yes_price is not None and yes_price <= _RESOLVED_NO_MAX:
                continue  # resolved NO → eliminated outcome, drops out of the partition
            return None  # winner / void / unknown closed price → can't prove a safe drop
        if not (market.is_binary and market.active and market.accepting_orders):
            return None  # partition hole → can't prove exhaustiveness
        live.append(market)
    # A single survivor is a near-certain winner, not a structural basket.
    return live if len(live) >= 2 else None


class NegRiskBasketDetector:
    kind: ClassVar[DetectorKind] = DetectorKind.NEGRISK_BASKET

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        event = snap.event
        if event is None or not event.is_multi_outcome:
            return
        live = live_partition(event)
        if live is None:
            return

        token_ids: list[str] = []
        outcomes: list[str] = []
        ask_levels: list[list[BookLevel]] = []
        fee_rates: list[Decimal] = []
        condition_ids: list[str] = []
        for market in live:
            book: OrderBook | None = snap.books.get(market.yes_token_id)
            if book is None or book.best_ask is None or is_crossed(book):
                return  # missing/stale book on a live outcome → partition incomplete; skip
            token_ids.append(market.yes_token_id)
            outcomes.append(market.group_item_title or market.outcomes[0])
            ask_levels.append(book.asks)
            fee_rates.append(fee_rate_for(market))
            condition_ids.append(market.condition_id)

        # Depth-walk every live outcome's YES asks: buy one share of each per set; exactly one
        # outcome pays 1 at resolution, so a completed set is worth payoff=1. None ⇒ no
        # profitable depth or doesn't clear gas.
        result = walk_and_size_buy_basket(ask_levels, fee_rates, snap.gas)
        if result is None:
            return
        size, leg_costs, profit = result

        legs = [
            Leg(
                token_id=token_ids[i],
                side="buy",
                price=leg_costs[i] / size,
                size=size,
                outcome=outcomes[i],
            )
            for i in range(len(token_ids))
        ]

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
            description=f"negrisk basket under: {event.title.strip()} (Σ YES = {profit.cost})",
            condition_ids=condition_ids,
            legs=legs,
            profit=profit,
            executable_size=size,
            realizes="resolution",
            event_id=event.id,
            days_to_resolution=days,
            gas=snap.gas,
        )
