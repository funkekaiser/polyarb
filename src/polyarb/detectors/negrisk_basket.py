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
from polyarb.pricing.sizing import is_crossed, top_level_min_depth
from polyarb.resolution.risk import ResolutionRisk, classify_market

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


def dual_profit(no_asks: list[Decimal], fee_rates: list[Decimal]) -> Profit:
    """Profit from buying 1 NO of every outcome — the NO-basket dual (B3 / HEDGING §2).

    Across ``M`` mutually-exclusive outcomes exactly ``M-1`` resolve NO, so a set of one NO per
    outcome pays a guaranteed ``M-1``. Cost = Σ no_asks; payoff = ``M-1`` (M = len(no_asks)).
    Unlike the YES basket this needs only mutual exclusivity, not full exhaustiveness.
    """
    payoff = Decimal(len(no_asks) - 1)
    cost = sum(no_asks, ZERO)
    gross = payoff - cost
    fees = sum((taker_fee(p, ONE, r) for p, r in zip(no_asks, fee_rates, strict=True)), ZERO)
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
        # profitable depth or doesn't clear gas (scaled by leg count, B2').
        gas = snap.gas_for(len(token_ids))
        result = walk_and_size_buy_basket(ask_levels, fee_rates, gas)
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

        yield make_opportunity(
            detector=self.kind,
            description=f"negrisk basket under: {event.title.strip()} (Σ YES = {profit.cost})",
            condition_ids=condition_ids,
            legs=legs,
            profit=profit,
            executable_size=size,
            conservative_size=top_level_min_depth(ask_levels, side="buy"),
            realizes="resolution",
            event_id=event.id,
            days_by_condition=snap.days_to_resolution,
            gas=gas,
        )


class NegRiskDualDetector:
    """NO-basket dual — buy 1 NO of every live outcome; exactly M-1 resolve NO → payoff M-1.

    Arb if ``Σ a_no,i < M-1`` (net of fees + gas). Hedging coverage for when the YES basket is
    infeasible/edge-eroded (docs/HEDGING.md §2). The floor needs only **mutual exclusivity**
    (NegRisk guarantees ≤1 YES), so the augmented-skip is opted out via
    ``live_partition(skip_augmented=False)`` (an added/dropped outcome can't reduce the "M-1 of
    M lose" count). It still *conservatively* reuses live_partition's other gates (a closed
    winner/void/unknown leg → skip the whole event): those are safe false-negatives for the
    dual, so we accept the lost coverage rather than special-case them.

    **Void gate (committee CRITICAL).** Unlike the YES basket, the dual's floor is *not* robust
    to a 50-50 **void**: a losing leg that voids pays its NO $0.50 instead of $1, a -$0.50 hit —
    and there are M-1 losers, so the dual is ~(M-1)x more void-exposed, exactly when its edge is
    thinnest (~1/(M-1) of the YES side). A single void can exceed the whole edge. Since
    void-proneness isn't otherwise detectable (A2), we only emit where the floor is robust:
    **every live leg must resolve on a void-resistant (OBJECTIVE) source**; void-prone events
    are refused. Economics are inverted vs the YES basket: it deploys ≈M-1 capital for the *same
    absolute edge*, so its return-on-capital is far lower (it does not co-occur with a feasible
    YES basket on the same event — their edges are ≈ negatives — so this is about coverage, not
    out-ranking; ranking is by absolute net $, C3).
    """

    kind: ClassVar[DetectorKind] = DetectorKind.NEGRISK_DUAL

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        event = snap.event
        if event is None or not event.is_multi_outcome:
            return
        # Mutual exclusivity is enough for the M-1 floor, so don't skip augmented events.
        live = live_partition(event, skip_augmented=False)
        if live is None:
            return
        # Void gate: the dual's floor breaks under a losing leg's 50-50 void (see class docstring),
        # so only emit when every live leg resolves on a void-resistant (OBJECTIVE) source.
        if any(classify_market(m) != ResolutionRisk.OBJECTIVE for m in live):
            return

        token_ids: list[str] = []
        outcomes: list[str] = []
        ask_levels: list[list[BookLevel]] = []
        fee_rates: list[Decimal] = []
        condition_ids: list[str] = []
        for market in live:
            book: OrderBook | None = snap.books.get(market.no_token_id)
            if book is None or book.best_ask is None or is_crossed(book):
                return  # missing/stale NO book on a live outcome → can't lock the dual; skip
            token_ids.append(market.no_token_id)
            outcomes.append(market.group_item_title or market.outcomes[0])
            ask_levels.append(book.asks)
            fee_rates.append(fee_rate_for(market))
            condition_ids.append(market.condition_id)

        # Depth-walk every live outcome's NO asks: buy one NO of each per set; exactly M-1 of the
        # M outcomes resolve NO, so a completed set is worth payoff = M-1. None ⇒ no profitable
        # depth or doesn't clear gas.
        payoff = Decimal(len(token_ids) - 1)
        gas = snap.gas_for(len(token_ids))
        result = walk_and_size_buy_basket(ask_levels, fee_rates, gas, payoff=payoff)
        if result is None:
            return
        size, leg_costs, profit = result

        legs = [
            Leg(
                token_id=token_ids[i],
                side="buy",
                price=leg_costs[i] / size,
                size=size,
                outcome=f"No: {outcomes[i]}",
            )
            for i in range(len(token_ids))
        ]

        yield make_opportunity(
            detector=self.kind,
            description=f"negrisk NO-dual: {event.title.strip()} (Σ NO={profit.cost})",
            condition_ids=condition_ids,
            legs=legs,
            profit=profit,
            executable_size=size,
            conservative_size=top_level_min_depth(ask_levels, side="buy"),
            realizes="resolution",
            event_id=event.id,
            days_by_condition=snap.days_to_resolution,
            gas=gas,
        )
