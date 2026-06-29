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

from polyarb.detectors.base import ONE, ZERO, Profit, Snapshot, make_opportunity
from polyarb.models import BookLevel, DetectorKind, Leg, Opportunity, OrderBook
from polyarb.pricing.fees import fee_rate_for, taker_fee
from polyarb.pricing.sizing import is_crossed, walk_buy_legs

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


class NegRiskBasketDetector:
    kind: ClassVar[DetectorKind] = DetectorKind.NEGRISK_BASKET

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        event = snap.event
        if event is None or not event.is_multi_outcome:
            return
        # A1 — exhaustiveness. The "exactly one pays 1" guarantee holds only if the legs we buy
        # form a complete, mutually-exclusive, exhaustive partition. A *fixed* (non-augmented)
        # negRisk event is exhaustive by construction; an **augmented** one is not safely so —
        # outcomes can be added after a basket is locked and the "Other" leg's meaning shifts,
        # so Σ<1 there may be a correctly-priced incomplete basket, not an arb. Skip augmented.
        if event.neg_risk_augmented:
            return
        # Defense-in-depth (the scanner already pre-filters to open events): never build a basket
        # on a decided or inactive event — its "live" set need not contain the winner.
        if event.closed or not event.active:
            return

        token_ids: list[str] = []
        outcomes: list[str] = []
        ask_levels: list[list[BookLevel]] = []
        fee_rates: list[Decimal] = []
        condition_ids: list[str] = []

        for market in event.markets:
            # A 'closed' constituent has resolved — but NOT necessarily to NO, and the winner
            # closes first. Drop it from the partition only if its resolved YES price proves it
            # LOST (~0); the remaining live outcomes then still form a complete exhaustive set.
            # If it won (~1) the event is decided, if it voided (~0.5) the floor is broken, and
            # if its price is unknown we can't prove a safe drop — in every such case skip the
            # whole event rather than emit a basket whose true winner we've discarded ($0 payoff).
            if market.closed:
                yes_price = market.outcome_prices[0] if market.outcome_prices else None
                if yes_price is not None and yes_price <= _RESOLVED_NO_MAX:
                    continue  # resolved NO → eliminated outcome, drops out of the partition
                return
            # Any *live* constituent that isn't cleanly tradeable is a hole in the partition: we
            # can't prove exhaustiveness, so we must not emit a "guaranteed $1" basket. Skip the
            # whole event rather than buy a subset that pays $0 if the missing outcome wins.
            if not (market.is_binary and market.active and market.accepting_orders):
                return
            book: OrderBook | None = snap.books.get(market.yes_token_id)
            if book is None or book.best_ask is None or is_crossed(book):
                return  # missing/stale book on a live outcome → partition incomplete; skip
            token_ids.append(market.yes_token_id)
            outcomes.append(market.group_item_title or market.outcomes[0])
            ask_levels.append(book.asks)
            fee_rates.append(fee_rate_for(market))
            condition_ids.append(market.condition_id)

        # Need at least two live outcomes for a basket (after eliminations). A single survivor
        # is a near-certain winner, not a structural basket — leave it to avoid resolution-lag
        # directional bets masquerading as arbs.
        if len(token_ids) < 2:
            return

        # Depth-walk across every live outcome's YES asks: buy one share of each per set; exactly
        # one outcome pays 1 at resolution, so a completed set is worth payoff=1.
        size, leg_costs, fees = walk_buy_legs(ask_levels, fee_rates, payoff=ONE)
        if size <= ZERO:
            return
        cost_ps = sum(leg_costs, ZERO) / size
        profit = Profit(cost=cost_ps, gross_profit=ONE - cost_ps, fees=fees / size)
        # Emit only when the trade clears the fixed per-execution gas cost.
        if size * profit.net_profit - snap.gas <= ZERO:
            return

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
