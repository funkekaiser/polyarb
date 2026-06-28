"""Complement arbitrage — within a single binary market, YES + NO ≠ 1.

Realizes instantly via the split/merge mechanism (no resolution wait):

- **Under** (``a_yes + a_no < 1``): buy 1 YES + 1 NO, **merge** the pair → receive 1.
  ``net_profit = 1 - (a_yes + a_no) - f - g``
- **Over** (``b_yes + b_no > 1``): **split** 1 collateral → 1 YES + 1 NO, sell both legs.
  ``net_profit = (b_yes + b_no) - 1 - f - g``

``a_*`` are best asks (what you pay to buy), ``b_*`` are best bids (what you receive to sell).
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import ClassVar

from polyarb.detectors.base import ONE, ZERO, Profit, Snapshot, make_opportunity
from polyarb.models import DetectorKind, Leg, Opportunity
from polyarb.pricing.fees import fee_rate_for, taker_fee
from polyarb.pricing.sizing import depth_at_or_better, executable_size


def under_profit(a_yes: Decimal, a_no: Decimal, fee_rate: Decimal, gas: Decimal) -> Profit:
    """Buy YES+NO then merge. Cost = a_yes + a_no; redeem the merged set for 1."""
    cost = a_yes + a_no
    gross = ONE - cost
    fees = taker_fee(a_yes, ONE, fee_rate) + taker_fee(a_no, ONE, fee_rate)
    return Profit(cost=cost, gross_profit=gross, fees=fees, gas=gas)


def over_profit(b_yes: Decimal, b_no: Decimal, fee_rate: Decimal, gas: Decimal) -> Profit:
    """Split 1 collateral into YES+NO and sell both. Proceeds = b_yes + b_no; cost = 1."""
    proceeds = b_yes + b_no
    gross = proceeds - ONE
    fees = taker_fee(b_yes, ONE, fee_rate) + taker_fee(b_no, ONE, fee_rate)
    return Profit(cost=ONE, gross_profit=gross, fees=fees, gas=gas)


class ComplementDetector:
    kind: ClassVar[DetectorKind] = DetectorKind.COMPLEMENT

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        for market in snap.markets:
            if not market.is_binary:
                continue
            yes_book = snap.books.get(market.yes_token_id)
            no_book = snap.books.get(market.no_token_id)
            if yes_book is None or no_book is None:
                continue
            fee_rate = fee_rate_for(market)

            # Under: buy both asks, merge.
            a_yes, a_no = yes_book.best_ask, no_book.best_ask
            if a_yes is not None and a_no is not None:
                profit = under_profit(a_yes.price, a_no.price, fee_rate, snap.gas)
                if profit.net_profit > ZERO:
                    size = executable_size(
                        [
                            depth_at_or_better(yes_book, "buy", a_yes.price),
                            depth_at_or_better(no_book, "buy", a_no.price),
                        ]
                    )
                    yield make_opportunity(
                        detector=self.kind,
                        description=f"complement under: {market.question}",
                        condition_ids=[market.condition_id],
                        legs=[
                            Leg(
                                token_id=market.yes_token_id,
                                side="buy",
                                price=a_yes.price,
                                size=ONE,
                                outcome="Yes",
                            ),
                            Leg(
                                token_id=market.no_token_id,
                                side="buy",
                                price=a_no.price,
                                size=ONE,
                                outcome="No",
                            ),
                        ],
                        profit=profit,
                        executable_size=size,
                        realizes="instant",
                    )

            # Over: split collateral, sell both bids.
            b_yes, b_no = yes_book.best_bid, no_book.best_bid
            if b_yes is not None and b_no is not None:
                profit = over_profit(b_yes.price, b_no.price, fee_rate, snap.gas)
                if profit.net_profit > ZERO:
                    size = executable_size(
                        [
                            depth_at_or_better(yes_book, "sell", b_yes.price),
                            depth_at_or_better(no_book, "sell", b_no.price),
                        ]
                    )
                    yield make_opportunity(
                        detector=self.kind,
                        description=f"complement over: {market.question}",
                        condition_ids=[market.condition_id],
                        legs=[
                            Leg(
                                token_id=market.yes_token_id,
                                side="sell",
                                price=b_yes.price,
                                size=ONE,
                                outcome="Yes",
                            ),
                            Leg(
                                token_id=market.no_token_id,
                                side="sell",
                                price=b_no.price,
                                size=ONE,
                                outcome="No",
                            ),
                        ],
                        profit=profit,
                        executable_size=size,
                        realizes="instant",
                    )
