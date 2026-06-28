"""Fee model — net-of-fees profit.

Verified taker-fee formula (docs/API_NOTES.md): makers pay nothing; the taker fee per leg is

    fee = C * feeRate * p * (1 - p)

where ``C`` = shares traded and ``p`` = the share price in [0, 1]. The fee is parabolic:
zero at the price extremes, maximal at p=0.50. Fee-free categories (e.g. geopolitics) have
``feeRate = 0``. Per-market ``feeRate`` lives on the Gamma market object (``feeSchedule.rate``,
surfaced as ``Market.fee_rate``).
"""

from __future__ import annotations

from decimal import Decimal

from polyarb.models import Market

ZERO = Decimal(0)
ONE = Decimal(1)


def taker_fee(price: Decimal, size: Decimal, fee_rate: Decimal) -> Decimal:
    """Taker fee for buying/selling ``size`` shares at ``price`` under ``fee_rate``.

    Returns 0 for fee-free markets (``fee_rate == 0``) and at the price extremes.
    """
    if fee_rate <= ZERO or size <= ZERO:
        return ZERO
    if price <= ZERO or price >= ONE:
        # Fee is 0 at the bounds; outside [0,1] the parabola would go NEGATIVE (which would
        # inflate net profit), so guard against malformed prices.
        return ZERO
    return size * fee_rate * price * (ONE - price)


def fee_rate_for(market: Market) -> Decimal:
    """The effective taker fee rate for a market (0 when fee-free)."""
    if market.is_fee_free or market.fee_rate is None:
        return ZERO
    return market.fee_rate
