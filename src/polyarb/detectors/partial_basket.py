"""Partial NegRisk basket (§5) — OPT-IN, DIRECTIONAL, NOT a structural arb.

When the full NegRisk basket *would* be a structural arb (`T = Σ all live YES asks < 1`) but a
live leg is **unbuyable** (no usable book), you can't lock the whole thing. This detector
salvages the **buyable subset** `S` as a *directional bet* on the market-implied residual:
buying 1 YES of each S-leg pays $1 iff the winner ∈ S, else $0.

This is **not model-free** — it prices the un-bought outcomes with the market's own implied
probabilities, which is a forecasting assumption a structural identity never makes
(docs/HEDGING.md §5). It is therefore:

- **off by default** (``Settings.enable_partial_baskets``), never on the default scan path;
- tagged ``ResolutionRisk.DIRECTIONAL`` so it ranks below *every* structural arb;
- scored on an **expected value**, with the **worst-case loss = the full stake** surfaced.

EV math (see HEDGING §5). With ``T = Σ_all-live a_yes`` and ``S ⊊ live``, the normalized
implied win-probability of S is ``p = Σ_S / T``. Per set, payoff is $1 with prob p and $0
otherwise, at VWAP cost ``cost_ps``, so ``EV/set = p - cost_ps``. We size S by walking its books
while the marginal VWAP stays below ``p`` (``payoff=p``) and emit only if the total EV clears
gas. Two honesty caveats (committee):

1. **The EV is OPTIMISTIC, not a lower bound.** Pricing S^c at its (cached, often stale) best
   ask only buys a half-spread cushion; the dropped legs are precisely the illiquid ones whose
   *true* probability tends to exceed their stale ask (adverse selection), which **understates**
   T and **overstates** p. So treat EV as an upper-ish estimate, not a floor — the only hard
   number is the worst-case loss (full stake when winner ∉ S).
2. **``executable_size`` here is the risk-NEUTRAL max-EV size** (walk to marginal EV = 0); it
   ignores variance. A prudent (e.g. Kelly) bettor would size far smaller — treat it as an
   upper bound on size, not a recommendation.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import ClassVar

from polyarb.detectors.base import ONE, ZERO, Snapshot, make_opportunity, walk_and_size_buy_basket
from polyarb.detectors.negrisk_basket import live_partition
from polyarb.models import BookLevel, DetectorKind, Leg, Opportunity
from polyarb.pricing.fees import fee_rate_for
from polyarb.pricing.sizing import is_crossed


class PartialBasketDetector:
    kind: ClassVar[DetectorKind] = DetectorKind.PARTIAL_BASKET

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        event = snap.event
        if event is None or not event.is_multi_outcome:
            return
        # Need a stable, exhaustive partition for the residual estimate, so reuse the default
        # (augmented-skipping) live_partition; eliminated outcomes are already dropped from T.
        live = live_partition(event)
        if live is None:
            return

        token_ids: list[str] = []
        outcomes: list[str] = []
        ask_levels: list[list[BookLevel]] = []
        fee_rates: list[Decimal] = []
        condition_ids: list[str] = []
        sum_s_best = ZERO  # Σ best ask over the buyable subset S
        total = ZERO  # T = Σ best ask over ALL live outcomes (buyable + unbuyable)
        unbuyable = 0
        for market in live:
            book = snap.books.get(market.yes_token_id)
            if book is not None and book.best_ask is not None and not is_crossed(book):
                token_ids.append(market.yes_token_id)
                outcomes.append(market.group_item_title or market.outcomes[0])
                ask_levels.append(book.asks)
                fee_rates.append(fee_rate_for(market))
                condition_ids.append(market.condition_id)
                sum_s_best += book.best_ask.price
                total += book.best_ask.price
            else:
                # Unbuyable leg: price the residual from Gamma's cached YES ask (blended with the
                # live books at a different timestamp — A3 freshness isn't applied to this cached
                # value, see HEDGING §5). A None *or non-positive* ask → refuse: we can't estimate
                # the residual mass (and Gamma sends "0", not null, for an empty book, which would
                # zero the residual and collapse p to 1, faking full coverage of a subset).
                if market.best_ask is None or market.best_ask <= ZERO:
                    return
                total += market.best_ask
                unbuyable += 1

        # Only a *partial* case: if every live leg is buyable, the structural basket handles it.
        if unbuyable == 0 or len(token_ids) < 2:
            return
        # Require real slack (T < 1): the full set must itself be an underpriced structural arb,
        # otherwise this is pure directional speculation, not salvaging an unfillable lock.
        if total >= ONE or sum_s_best <= ZERO:
            return

        p = sum_s_best / total  # market-implied P(winner ∈ S)
        if p >= ONE:
            return  # no residual mass ⇒ not a partial bet (enforces the 0 < p < 1 invariant)
        # Size S where the marginal VWAP stays below p (payoff=p) → EV/set = p - cost_ps > 0,
        # and the total EV must clear the per-execution gas.
        gas = snap.gas_for(len(token_ids))  # buyable subset size = N taker fills (B2')
        result = walk_and_size_buy_basket(ask_levels, fee_rates, gas, payoff=p)
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
        n_total = len(token_ids) + unbuyable
        max_loss = profit.cost * size  # full stake, lost when the winner is in the dropped set
        yield make_opportunity(
            detector=self.kind,
            description=(
                f"partial basket (DIRECTIONAL, not structural): {event.title.strip()} — "
                f"buy {len(token_ids)}/{n_total} legs, p~{p}, EV/set~{profit.net_profit}, "
                f"max loss ~{max_loss} (winner not in subset)"
            ),
            condition_ids=condition_ids,
            legs=legs,
            profit=profit,
            executable_size=size,
            realizes="resolution",
            event_id=event.id,
            days_to_resolution=days,
            gas=gas,
        )
