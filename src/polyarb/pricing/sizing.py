"""Executable sizing from order-book depth.

An arb is only real if you can actually fill it. ``depth_at_or_better`` sums the size
available at or better than a limit price on one side of a book; the executable size of a
multi-leg arb is the *minimum* such depth across its legs (you can only do as many sets as
the thinnest leg supports). The engine (Phase 3) rejects opportunities whose executable
notional falls below ``MIN_NOTIONAL`` — never report an arb that exists for one share.

``walk_buy_legs`` and ``walk_sell_legs`` perform a joint depth-walk across multiple legs,
capturing all profitable depth and returning VWAP economics. Used by the complement detector.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from polyarb.models import BookLevel, OrderBook
from polyarb.pricing.fees import taker_fee

ZERO = Decimal(0)
ONE = Decimal(1)


def depth_at_or_better(book: OrderBook, side: str, limit_price: Decimal) -> Decimal:
    """Cumulative size you can transact at ``limit_price`` or better on ``side``.

    - ``side="buy"`` consumes ASKS priced at or below ``limit_price`` (you pay no more).
    - ``side="sell"`` consumes BIDS priced at or above ``limit_price`` (you receive no less).
    """
    if side == "buy":
        return sum((lvl.size for lvl in book.asks if lvl.price <= limit_price), ZERO)
    if side == "sell":
        return sum((lvl.size for lvl in book.bids if lvl.price >= limit_price), ZERO)
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


def executable_size(depths: list[Decimal]) -> Decimal:
    """Sets supported by the thinnest leg. Empty → 0."""
    return min(depths, default=ZERO)


def top_level_min_depth(leg_levels: list[list[BookLevel]], *, side: str) -> Decimal:
    """Conservative one-shot size: min over legs of the size at each leg's *best* price level.

    The joint depth-walk's ``executable_size`` sweeps every profitable level assuming an atomic
    multi-leg fill (an optimistic ceiling). This is the pessimistic companion (C1-atomicity): how
    many sets you could plausibly grab at the single best level of every leg simultaneously.
    ``side="buy"`` → best = lowest ask price; ``side="sell"`` → best = highest bid price.
    Non-positive price/size levels are ignored; a leg with no valid level → 0.
    """
    sizes: list[Decimal] = []
    for levels in leg_levels:
        valid = [lvl for lvl in levels if lvl.size > ZERO and lvl.price > ZERO]
        if not valid:
            return ZERO
        best = (
            min(valid, key=lambda lvl: lvl.price)
            if side == "buy"
            else max(valid, key=lambda lvl: lvl.price)
        )
        sizes.append(best.size)
    return min(sizes, default=ZERO)


def is_crossed(book: OrderBook) -> bool:
    """Return True if ``book`` is crossed (best bid >= best ask).

    A crossed book signals stale or erroneous data; both sides are required to be present.
    Non-positive prices/sizes are ignored (they are bad-payload artefacts, not real quotes).
    """
    bids = [lvl for lvl in book.bids if lvl.size > ZERO and lvl.price > ZERO]
    asks = [lvl for lvl in book.asks if lvl.size > ZERO and lvl.price > ZERO]
    if not bids or not asks:
        return False
    best_bid = max(bids, key=lambda lvl: lvl.price)
    best_ask = min(asks, key=lambda lvl: lvl.price)
    return best_bid.price >= best_ask.price


def _per_leg_rates(fee_rate: Decimal | Sequence[Decimal], n_legs: int) -> list[Decimal]:
    """Normalize a scalar-or-sequence fee rate to one rate per leg.

    A scalar is broadcast to every leg; a sequence must have exactly ``n_legs`` entries.
    """
    if isinstance(fee_rate, Decimal):
        return [fee_rate] * n_legs
    rates = list(fee_rate)
    if len(rates) != n_legs:
        raise ValueError(f"fee_rate has {len(rates)} entries, expected {n_legs} (one per leg)")
    return rates


def walk_buy_legs(
    leg_levels: list[list[BookLevel]],
    fee_rate: Decimal | Sequence[Decimal],
    payoff: Decimal = ONE,
) -> tuple[Decimal, list[Decimal], Decimal]:
    """Greedily size a 'buy one share from each leg per set, completed set is worth `payoff`'
    arb across cumulative book depth.

    Fill each leg cheapest-first (asks ascending). The s-th set pairs the s-th-cheapest share
    of every leg; its marginal cost = sum of those per-leg prices, plus per-share taker fees.
    Include sets while marginal_cost + marginal_fee < payoff (strictly). Because prices are
    non-decreasing, marginal cost is non-decreasing, so the walk stops cleanly.

    ``fee_rate`` may be a single rate applied to every leg, or a per-leg sequence (one rate
    per leg, in ``leg_levels`` order) when legs span different markets/fee categories. A
    per-leg sequence whose length differs from the number of legs is a programming error.

    Returns (size, per_leg_cost, total_fees):
      size         = total sets includable
      per_leg_cost = total spent on each leg over ``size`` sets (same order as leg_levels)
      total_fees   = total taker fees over ``size`` sets
    Empty/unprofitable → (ZERO, [ZERO]*len(legs), ZERO).
    """
    n_legs = len(leg_levels)
    rates = _per_leg_rates(fee_rate, n_legs)
    zero_result: tuple[Decimal, list[Decimal], Decimal] = (ZERO, [ZERO] * n_legs, ZERO)
    if n_legs == 0:
        return zero_result  # no legs → nothing to size (avoid min() on an empty range below)

    # Filter zero-size and non-positive-price levels; sort each leg's asks ascending.
    sorted_legs: list[list[BookLevel]] = []
    for levels in leg_levels:
        filtered = sorted(
            (lvl for lvl in levels if lvl.size > ZERO and lvl.price > ZERO),
            key=lambda lvl: lvl.price,
        )
        if not filtered:
            return zero_result
        sorted_legs.append(filtered)

    # State: current level index and remaining fillable size at that level, per leg.
    idx = [0] * n_legs
    remaining = [sorted_legs[i][0].size for i in range(n_legs)]

    total_size = ZERO
    per_leg_cost: list[Decimal] = [ZERO] * n_legs
    total_fees = ZERO

    while True:
        # Stop when any leg has no more depth.
        if any(idx[i] >= len(sorted_legs[i]) for i in range(n_legs)):
            break

        prices = [sorted_legs[i][idx[i]].price for i in range(n_legs)]
        marginal_fee = sum((taker_fee(prices[i], ONE, rates[i]) for i in range(n_legs)), ZERO)

        # Include this price slice only while it is strictly profitable.
        if sum(prices, ZERO) + marginal_fee >= payoff:
            break

        chunk = min(remaining[i] for i in range(n_legs))

        total_size += chunk
        for i in range(n_legs):
            per_leg_cost[i] += chunk * prices[i]
            remaining[i] -= chunk
            if remaining[i] == ZERO:
                idx[i] += 1
                if idx[i] < len(sorted_legs[i]):
                    remaining[i] = sorted_legs[i][idx[i]].size
        total_fees += chunk * marginal_fee

    return total_size, per_leg_cost, total_fees


def walk_sell_legs(
    leg_levels: list[list[BookLevel]],
    fee_rate: Decimal,
    collateral: Decimal = ONE,
) -> tuple[Decimal, list[Decimal], Decimal]:
    """Size a 'split `collateral` into one share of each leg, sell each at bids' arb.

    Fill each leg's bids best-first (DESCENDING price). The s-th set's proceeds = sum of the
    s-th-best bid across legs; include while proceeds - marginal_fee > collateral (strictly).

    Returns (size, per_leg_proceeds, total_fees):
      size             = total sets includable
      per_leg_proceeds = total received on each leg over ``size`` sets
      total_fees       = total taker fees over ``size`` sets
    Empty/unprofitable → (ZERO, [ZERO]*len(legs), ZERO).
    """
    n_legs = len(leg_levels)
    zero_result: tuple[Decimal, list[Decimal], Decimal] = (ZERO, [ZERO] * n_legs, ZERO)

    # Filter zero-size and non-positive-price levels; sort each leg's bids descending.
    sorted_legs: list[list[BookLevel]] = []
    for levels in leg_levels:
        filtered = sorted(
            (lvl for lvl in levels if lvl.size > ZERO and lvl.price > ZERO),
            key=lambda lvl: lvl.price,
            reverse=True,
        )
        if not filtered:
            return zero_result
        sorted_legs.append(filtered)

    idx = [0] * n_legs
    remaining = [sorted_legs[i][0].size for i in range(n_legs)]

    total_size = ZERO
    per_leg_proceeds: list[Decimal] = [ZERO] * n_legs
    total_fees = ZERO

    while True:
        if any(idx[i] >= len(sorted_legs[i]) for i in range(n_legs)):
            break

        prices = [sorted_legs[i][idx[i]].price for i in range(n_legs)]
        marginal_fee = sum((taker_fee(p, ONE, fee_rate) for p in prices), ZERO)

        # Include this price slice only while proceeds strictly exceed collateral + fees.
        if sum(prices, ZERO) - marginal_fee <= collateral:
            break

        chunk = min(remaining[i] for i in range(n_legs))

        total_size += chunk
        for i in range(n_legs):
            per_leg_proceeds[i] += chunk * prices[i]
            remaining[i] -= chunk
            if remaining[i] == ZERO:
                idx[i] += 1
                if idx[i] < len(sorted_legs[i]):
                    remaining[i] = sorted_legs[i][idx[i]].size
        total_fees += chunk * marginal_fee

    return total_size, per_leg_proceeds, total_fees
