"""Executable sizing from order-book depth.

An arb is only real if you can actually fill it. ``depth_at_or_better`` sums the size
available at or better than a limit price on one side of a book; the executable size of a
multi-leg arb is the *minimum* such depth across its legs (you can only do as many sets as
the thinnest leg supports). The engine (Phase 3) rejects opportunities whose executable
notional falls below ``MIN_NOTIONAL`` — never report an arb that exists for one share.
"""

from __future__ import annotations

from decimal import Decimal

from polyarb.models import OrderBook

ZERO = Decimal(0)


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
