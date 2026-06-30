"""Book-quality guards — detect structurally corrupt order-book snapshots.

Provides :func:`is_corrupt_book`, a conservative predicate for the **#180 corrupt-book
pattern** observed in live Polymarket CLOB snapshots: a stale snapshot that has collapsed to
a degenerate full-width extreme spread (best bid ≤ 0.01 **and** best ask ≥ 0.99) with no
real intermediate liquidity.

This module depends only on :mod:`polyarb.models` (OrderBook/BookLevel) — no imports that
would create cycles with the detector or pricing layers.
"""

from __future__ import annotations

from decimal import Decimal

from polyarb.models import OrderBook

_ZERO = Decimal(0)

# Thresholds for the #180 degenerate-extreme-spread pattern.
# 0.01 is the minimum Polymarket price tick; 0.99 is one tick below 1.
# Any legitimate book — however thin or longshot — will have either a bid above 0.01
# OR an ask below 0.99 (or both).  Only a frozen/stale snapshot parks *both* at once.
_CORRUPT_BID_MAX = Decimal("0.01")  # best valid bid AT OR BELOW this
_CORRUPT_ASK_MIN = Decimal("0.99")  # best valid ask AT OR ABOVE this


def is_corrupt_book(book: OrderBook) -> bool:
    """Return True if ``book`` shows the #180 degenerate-extreme-spread corrupt pattern.

    **The pattern** — a two-sided CLOB snapshot whose entire usable liquidity has collapsed
    to the full-width extreme: the best bid is pinned at or below 0.01 *and* the best ask is
    pinned at or above 0.99, with no valid level strictly inside (0.01, 0.99) on either side.
    This is a fingerprint of a stale or frozen snapshot (issue #180), not a legitimate thin
    or quiescent market.

    **Why this is not a quiescent book:**

    * A genuinely thin/longshot market has a *narrow* spread around its real probability.
      A 2%-chance event trades at roughly 0.02/0.04 (2¢ spread) — not 0.01/0.99.
    * A wide-spread book might show 0.40/0.60 or even 0.30/0.70.
    * Even the most thinly traded legitimate book has either a real bid above 0.01 *or* a
      real ask below 0.99.  Showing bid ≤ 0.01 **simultaneously with** ask ≥ 0.99 (a 98¢
      spread on a binary!) is only observed when the automated market-maker or last human
      quote has expired and the snapshot reflects degenerate resting orders from a much
      earlier, differently-priced session.

    **Conservative thresholds (false-negative biased):**

    * ``best_bid ≤ 0.01`` — 0.01 is Polymarket's minimum price tick, so this matches the
      lowest single valid bid.  Any book with a legitimate bid above 0.01 is *not* flagged.
    * ``best_ask ≥ 0.99`` — one tick below 1.  Any book with a legitimate ask below 0.99
      is *not* flagged.
    * The "no interior level" check (no valid level with 0.01 < price < 0.99 on either
      side) is implied by the best-price conditions above (best_bid = max of valid bids, so
      best_bid ≤ 0.01 means no valid bid above 0.01; best_ask = min of valid asks, so
      best_ask ≥ 0.99 means no valid ask below 0.99), but stated explicitly for clarity and
      defensive safety against future refactors.

    **Validity filter** — uses the same ``size > 0 and price > 0`` predicate as
    :meth:`~polyarb.models.OrderBook.best_bid`, :meth:`~polyarb.models.OrderBook.best_ask`,
    :func:`~polyarb.pricing.sizing.is_crossed`, and
    :func:`~polyarb.pricing.sizing.top_level_min_depth`.  Zero-size or non-positive-price
    levels are treated as phantom artefacts and ignored.

    **Error direction** — this predicate can only cause false *negatives* downstream
    (skipping a book that looks corrupt but might be a legitimate 0.01/0.99 extreme
    longshot).  It *never* causes false positives: skipping a book means the detector
    simply does not emit — identical to the detector having no book at all.

    Out of scope: hash-revert detection (flagging a book whose snapshot hash reverted to an
    earlier state across passes) — that requires cross-pass state and an ``OrderBook`` hash
    field and must remain stateful.  Keep this function stateless.
    """
    valid_bids = [lvl for lvl in book.bids if lvl.size > _ZERO and lvl.price > _ZERO]
    valid_asks = [lvl for lvl in book.asks if lvl.size > _ZERO and lvl.price > _ZERO]

    # A one-sided or empty book is *not* the #180 pattern.
    # is_crossed and the existing best_ask-is-None checks already handle those cases.
    if not valid_bids or not valid_asks:
        return False

    best_bid_price = max(lvl.price for lvl in valid_bids)
    best_ask_price = min(lvl.price for lvl in valid_asks)

    # Any legitimate bid above the minimum tick → real book, not corrupt.
    if best_bid_price > _CORRUPT_BID_MAX:
        return False
    # Any legitimate ask below the maximum tick → real book, not corrupt.
    if best_ask_price < _CORRUPT_ASK_MIN:
        return False

    # Redundant interior-level check: implied by the best-price conditions above
    # (best_bid ≤ 0.01 means no valid bid in (0.01, 0.99); best_ask ≥ 0.99 means no
    # valid ask in (0.01, 0.99)), but stated explicitly for defensive clarity.
    # Both conditions are unreachable given the best-price guards above, but the
    # negated-return form satisfies ruff SIM103 while keeping the explicit intent.
    no_interior_bid = not any(_CORRUPT_BID_MAX < lvl.price < _CORRUPT_ASK_MIN for lvl in valid_bids)
    no_interior_ask = not any(_CORRUPT_BID_MAX < lvl.price < _CORRUPT_ASK_MIN for lvl in valid_asks)
    return no_interior_bid and no_interior_ask
