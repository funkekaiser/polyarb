"""Logical-dependency arbitrage — across markets A, B where A ⇒ B (so P(A) ≤ P(B)).

When the identity is violated (``price(A) > price(B)``) you can lock a profit by buying
``YES_B`` + ``NO_A``:

    cost      = a_yes,B + a_no,A           (best asks of B-YES and A-NO)
    min payoff = 1                          (worst case A occurs ⇒ B occurs)
    net_profit ≥ price(A) - price(B) - f - g   (paid at resolution)

Relations are declared (``resolution.relations``), never inferred from text. This detector
uses the *actual* NO_A ask from the book rather than the ``1 - a_yes,A`` approximation.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import ClassVar

from polyarb.detectors.base import ONE, ZERO, Profit, Snapshot, make_opportunity
from polyarb.models import DetectorKind, Leg, Opportunity
from polyarb.pricing.fees import fee_rate_for, taker_fee
from polyarb.pricing.sizing import depth_at_or_better, executable_size


def dependency_profit(
    a_yes_b: Decimal,
    a_no_a: Decimal,
    fee_rate_b: Decimal,
    fee_rate_a: Decimal,
    gas: Decimal,
) -> Profit:
    """Buy YES_B + NO_A. Cost = a_yes_b + a_no_a; guaranteed min payoff = 1."""
    cost = a_yes_b + a_no_a
    gross = ONE - cost
    fees = taker_fee(a_yes_b, ONE, fee_rate_b) + taker_fee(a_no_a, ONE, fee_rate_a)
    return Profit(cost=cost, gross_profit=gross, fees=fees, gas=gas)


class DependencyDetector:
    kind: ClassVar[DetectorKind] = DetectorKind.DEPENDENCY

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        by_condition = {m.condition_id: m for m in snap.markets}
        for relation in snap.relations:
            market_a = by_condition.get(relation.antecedent_condition_id)
            market_b = by_condition.get(relation.consequent_condition_id)
            if market_a is None or market_b is None:
                continue
            if not (market_a.is_binary and market_b.is_binary):
                continue

            no_a_book = snap.books.get(market_a.no_token_id)
            yes_b_book = snap.books.get(market_b.yes_token_id)
            if no_a_book is None or yes_b_book is None:
                continue
            a_no_a = no_a_book.best_ask
            a_yes_b = yes_b_book.best_ask
            if a_no_a is None or a_yes_b is None:
                continue

            profit = dependency_profit(
                a_yes_b.price,
                a_no_a.price,
                fee_rate_for(market_b),
                fee_rate_for(market_a),
                snap.gas,
            )
            if profit.net_profit <= ZERO:
                continue

            size = executable_size(
                [
                    depth_at_or_better(yes_b_book, "buy", a_yes_b.price),
                    depth_at_or_better(no_a_book, "buy", a_no_a.price),
                ]
            )
            days = snap.days_to_resolution.get(
                market_b.condition_id
            ) or snap.days_to_resolution.get(market_a.condition_id)
            yield make_opportunity(
                detector=self.kind,
                description=f"dependency violation: {relation.description}",
                condition_ids=[market_a.condition_id, market_b.condition_id],
                legs=[
                    Leg(
                        token_id=market_b.yes_token_id,
                        side="buy",
                        price=a_yes_b.price,
                        size=ONE,
                        outcome="Yes_B",
                    ),
                    Leg(
                        token_id=market_a.no_token_id,
                        side="buy",
                        price=a_no_a.price,
                        size=ONE,
                        outcome="No_A",
                    ),
                ],
                profit=profit,
                executable_size=size,
                realizes="resolution",
                days_to_resolution=days,
            )
