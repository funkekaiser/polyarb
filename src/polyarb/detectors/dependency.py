"""Logical-dependency arbitrage — across markets A, B where A ⇒ B (so P(A) ≤ P(B)).

When the identity is violated (``price(A) > price(B)``) you can lock a profit by buying
``YES_B`` + ``NO_A``:

    cost               = a_yes,B + a_no,A           (best asks of B-YES and A-NO)
    min payoff         = 1                           (worst case A occurs ⇒ B occurs)
    net_profit (per set) ≥ price(A) - price(B) - f  (paid at resolution)
    total_net_profit   = size * net_profit - gas     (gas applied once at execution level)

Relations are declared (``resolution.relations``), never inferred from text. This detector
uses the *actual* NO_A ask from the book rather than the ``1 - a_yes,A`` approximation.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import ClassVar

from polyarb.detectors.base import (
    ONE,
    Profit,
    Snapshot,
    make_opportunity,
    walk_and_size_buy_basket,
)
from polyarb.models import DetectorKind, Leg, Opportunity
from polyarb.pricing.fees import fee_rate_for, taker_fee
from polyarb.pricing.sizing import is_crossed


def dependency_profit(
    a_yes_b: Decimal,
    a_no_a: Decimal,
    fee_rate_b: Decimal,
    fee_rate_a: Decimal,
) -> Profit:
    """Buy YES_B + NO_A. Cost = a_yes_b + a_no_a; guaranteed min payoff = 1."""
    cost = a_yes_b + a_no_a
    gross = ONE - cost
    fees = taker_fee(a_yes_b, ONE, fee_rate_b) + taker_fee(a_no_a, ONE, fee_rate_a)
    return Profit(cost=cost, gross_profit=gross, fees=fees)


class DependencyDetector:
    kind: ClassVar[DetectorKind] = DetectorKind.DEPENDENCY

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        by_condition = {m.condition_id: m for m in snap.markets}
        gas = snap.gas_for(2)  # dependency is always a 2-leg execution (YES_B + NO_A) (B2')
        for relation in snap.relations:
            if relation.antecedent_condition_id == relation.consequent_condition_id:
                continue  # a self-loop A⇒A is not a dependency (it would mis-label a complement)
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
            if no_a_book.best_ask is None or yes_b_book.best_ask is None:
                continue
            if is_crossed(no_a_book) or is_crossed(yes_b_book):
                continue  # stale/erroneous data; skip this relation

            # Depth-walk both legs (YES_B, NO_A): buy one share of each per set; the worst case
            # (A occurs ⇒ B occurs) guarantees a completed set is worth payoff=1. None ⇒ no
            # profitable depth or doesn't clear gas.
            result = walk_and_size_buy_basket(
                [yes_b_book.asks, no_a_book.asks],
                [fee_rate_for(market_b), fee_rate_for(market_a)],
                gas,
            )
            if result is None:
                continue
            size, leg_costs, profit = result

            # Use B's horizon, falling back to A's — but with an explicit None check, not
            # `or`: a legitimate days_to_resolution of 0 (resolves today) is falsy and would
            # otherwise be silently replaced by A's horizon, mis-annualizing the opp.
            days = snap.days_to_resolution.get(market_b.condition_id)
            if days is None:
                days = snap.days_to_resolution.get(market_a.condition_id)
            yield make_opportunity(
                detector=self.kind,
                description=f"dependency violation: {relation.description}",
                condition_ids=[market_a.condition_id, market_b.condition_id],
                legs=[
                    Leg(
                        token_id=market_b.yes_token_id,
                        side="buy",
                        price=leg_costs[0] / size,
                        size=size,
                        outcome="Yes_B",
                    ),
                    Leg(
                        token_id=market_a.no_token_id,
                        side="buy",
                        price=leg_costs[1] / size,
                        size=size,
                        outcome="No_A",
                    ),
                ],
                profit=profit,
                executable_size=size,
                realizes="resolution",
                days_to_resolution=days,
                gas=gas,
            )
