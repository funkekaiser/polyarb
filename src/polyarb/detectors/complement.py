"""Complement arbitrage — within a single binary market, YES + NO ≠ 1.

Realizes instantly via the split/merge mechanism (no resolution wait):

- **Under** (``a_yes + a_no < 1``): buy 1 YES + 1 NO, **merge** the pair → receive 1.
  ``net_profit (per set) = 1 - (a_yes + a_no) - f``
- **Over** (``b_yes + b_no > 1``): **split** 1 collateral → 1 YES + 1 NO, sell both legs.
  ``net_profit (per set) = (b_yes + b_no) - 1 - f``

Gas (one tx per execution) is applied at the execution level in ``make_opportunity``, not here.
``a_*`` are best asks (what you pay to buy), ``b_*`` are best bids (what you receive to sell).
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
from polyarb.models import DetectorKind, Leg, Opportunity
from polyarb.pricing.fees import fee_rate_for, taker_fee
from polyarb.pricing.sizing import is_crossed, top_level_min_depth, walk_sell_legs


def under_profit(a_yes: Decimal, a_no: Decimal, fee_rate: Decimal) -> Profit:
    """Buy YES+NO then merge. Cost = a_yes + a_no; redeem the merged set for 1."""
    cost = a_yes + a_no
    gross = ONE - cost
    fees = taker_fee(a_yes, ONE, fee_rate) + taker_fee(a_no, ONE, fee_rate)
    return Profit(cost=cost, gross_profit=gross, fees=fees)


def over_profit(b_yes: Decimal, b_no: Decimal, fee_rate: Decimal) -> Profit:
    """Split 1 collateral into YES+NO and sell both. Proceeds = b_yes + b_no; cost = 1."""
    proceeds = b_yes + b_no
    gross = proceeds - ONE
    fees = taker_fee(b_yes, ONE, fee_rate) + taker_fee(b_no, ONE, fee_rate)
    return Profit(cost=ONE, gross_profit=gross, fees=fees)


class ComplementDetector:
    kind: ClassVar[DetectorKind] = DetectorKind.COMPLEMENT

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        gas = snap.gas_for(2)  # complement is always a 2-leg execution (under or over) (B2')
        for market in snap.markets:
            if not market.is_binary:
                continue
            yes_book = snap.books.get(market.yes_token_id)
            no_book = snap.books.get(market.no_token_id)
            if yes_book is None or no_book is None:
                continue
            # Fix 2: skip markets with a crossed book (stale/erroneous data).
            if is_crossed(yes_book) or is_crossed(no_book):
                continue
            fee_rate = fee_rate_for(market)

            # Under: buy both asks across all profitable depth, merge. The helper applies the
            # gas-realizability guard; a None result leaves the over branch below reachable
            # (they're mutually exclusive on a non-crossed book, but kept independent).
            under = walk_and_size_buy_basket([yes_book.asks, no_book.asks], fee_rate, gas)
            if under is not None:
                size, leg_costs, profit = under
                yield make_opportunity(
                    detector=self.kind,
                    description=f"complement under: {market.question}",
                    condition_ids=[market.condition_id],
                    legs=[
                        Leg(
                            token_id=market.yes_token_id,
                            side="buy",
                            price=leg_costs[0] / size,
                            size=size,
                            outcome="Yes",
                        ),
                        Leg(
                            token_id=market.no_token_id,
                            side="buy",
                            price=leg_costs[1] / size,
                            size=size,
                            outcome="No",
                        ),
                    ],
                    profit=profit,
                    executable_size=size,
                    conservative_size=top_level_min_depth(
                        [yes_book.asks, no_book.asks], side="buy"
                    ),
                    realizes="instant",
                    gas=gas,
                )

            # Over: split collateral across all profitable depth, sell both bids.
            size, leg_proceeds, fees = walk_sell_legs([yes_book.bids, no_book.bids], fee_rate)
            if size > ZERO:
                proceeds_ps = sum(leg_proceeds, ZERO) / size
                profit = Profit(cost=ONE, gross_profit=proceeds_ps - ONE, fees=fees / size)
                # Fix 4: emit only when the trade clears the fixed per-execution gas cost.
                if size * profit.net_profit - gas > ZERO:
                    yield make_opportunity(
                        detector=self.kind,
                        description=f"complement over: {market.question}",
                        condition_ids=[market.condition_id],
                        legs=[
                            Leg(
                                token_id=market.yes_token_id,
                                side="sell",
                                price=leg_proceeds[0] / size,
                                size=size,
                                outcome="Yes",
                            ),
                            Leg(
                                token_id=market.no_token_id,
                                side="sell",
                                price=leg_proceeds[1] / size,
                                size=size,
                                outcome="No",
                            ),
                        ],
                        profit=profit,
                        executable_size=size,
                        conservative_size=top_level_min_depth(
                            [yes_book.bids, no_book.bids], side="sell"
                        ),
                        realizes="instant",
                        gas=gas,
                    )
