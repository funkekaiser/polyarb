"""Tests for the #180 corrupt-book detector (book_quality.is_corrupt_book).

Adversarial design: every False-returning case has been chosen to probe a specific
boundary — a genuine thin/longshot market that must NOT be gated out.  The True cases
are the exact degenerate pattern (or slight variants) that must always be caught.
"""

from __future__ import annotations

from decimal import Decimal

from polyarb.detectors.base import Snapshot
from polyarb.detectors.complement import ComplementDetector
from polyarb.pricing.book_quality import is_corrupt_book
from tests.helpers import make_book, make_market

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _corrupt() -> dict[str, object]:
    """Keyword args for make_book that produce the #180 corrupt pattern."""
    return {"bids": [("0.01", "100")], "asks": [("0.99", "100")]}


# ---------------------------------------------------------------------------
# True cases — must flag as corrupt
# ---------------------------------------------------------------------------


def test_classic_corrupt_both_extremes() -> None:
    """Canonical #180 pattern: single bid at 0.01, single ask at 0.99 → True."""
    book = make_book("t", bids=[("0.01", "100")], asks=[("0.99", "100")])
    assert is_corrupt_book(book) is True


def test_corrupt_bid_below_min_ask_at_ceiling() -> None:
    """best bid strictly below 0.01 and ask exactly 0.99 → still True."""
    book = make_book("t", bids=[("0.005", "50")], asks=[("0.99", "50")])
    assert is_corrupt_book(book) is True


def test_corrupt_bid_at_floor_ask_above_ceiling() -> None:
    """best bid exactly 0.01 and ask strictly above 0.99 → True."""
    book = make_book("t", bids=[("0.01", "50")], asks=[("0.995", "50")])
    assert is_corrupt_book(book) is True


def test_corrupt_multiple_levels_all_at_extremes() -> None:
    """Multiple levels, all at or below 0.01 on bids / at or above 0.99 on asks → True."""
    book = make_book(
        "t",
        bids=[("0.01", "100"), ("0.005", "50")],
        asks=[("0.99", "100"), ("0.995", "50")],
    )
    assert is_corrupt_book(book) is True


def test_corrupt_with_junk_zero_size_interior_levels_filtered_out() -> None:
    """Zero-size interior levels are phantom artefacts and must be ignored.

    The junk bid at 0.50 (size=0) and ask at 0.50 (size=0) look like interior
    levels but are filtered out by the validity predicate, leaving only the corrupt
    extremes — so the book must still be flagged True.
    """
    book = make_book(
        "t",
        bids=[("0.01", "100"), ("0.50", "0")],  # 0.50 bid has size=0 → phantom
        asks=[("0.99", "100"), ("0.50", "0")],  # 0.50 ask has size=0 → phantom
    )
    assert is_corrupt_book(book) is True


def test_corrupt_with_negative_price_interior_levels_filtered_out() -> None:
    """Non-positive-price interior levels are phantom artefacts and must be ignored."""
    book = make_book(
        "t",
        bids=[("0.01", "100"), ("-0.50", "100")],  # negative price → filtered
        asks=[("0.99", "100"), ("-0.50", "100")],  # negative price → filtered
    )
    assert is_corrupt_book(book) is True


# ---------------------------------------------------------------------------
# False cases — must NOT flag; each represents a real market scenario
# ---------------------------------------------------------------------------


def test_legit_balanced_market() -> None:
    """Normal balanced market (0.45/0.55) must not be flagged."""
    book = make_book("t", bids=[("0.45", "100")], asks=[("0.55", "100")])
    assert is_corrupt_book(book) is False


def test_legit_longshot_cheap_side() -> None:
    """Low-probability outcome (2% YES) with a narrow spread 0.02/0.04 → False.

    This is the most important false-negative guard: a thinly-traded longshot whose
    bid is 0.02 (above 0.01) must not be treated as corrupt.
    """
    book = make_book("t", bids=[("0.02", "10")], asks=[("0.04", "10")])
    assert is_corrupt_book(book) is False


def test_legit_near_certain_outcome() -> None:
    """High-probability outcome (96/98) with a narrow spread at the top → False."""
    book = make_book("t", bids=[("0.96", "10")], asks=[("0.98", "10")])
    assert is_corrupt_book(book) is False


def test_legit_ask_just_below_ceiling() -> None:
    """Ask at 0.98 (below 0.99) → not corrupt even if bid is very low."""
    book = make_book("t", bids=[("0.01", "100")], asks=[("0.98", "100")])
    assert is_corrupt_book(book) is False


def test_legit_bid_just_above_floor() -> None:
    """Bid at 0.02 (above 0.01) → not corrupt even if ask is very high."""
    book = make_book("t", bids=[("0.02", "100")], asks=[("0.99", "100")])
    assert is_corrupt_book(book) is False


def test_legit_interior_bid_plus_extreme_ask() -> None:
    """Book with bid levels at BOTH 0.01 AND 0.40, ask at 0.99.

    best_bid = max(0.01, 0.40) = 0.40 > 0.01 → not corrupt.
    (This is the 'interior level present' scenario from the spec.)
    """
    book = make_book(
        "t",
        bids=[("0.01", "10"), ("0.40", "50")],
        asks=[("0.99", "100")],
    )
    assert is_corrupt_book(book) is False


def test_legit_extreme_bid_plus_interior_ask() -> None:
    """Bid at 0.01 and asks at BOTH 0.60 AND 0.99.

    best_ask = min(0.60, 0.99) = 0.60 < 0.99 → not corrupt.
    """
    book = make_book(
        "t",
        bids=[("0.01", "100")],
        asks=[("0.60", "50"), ("0.99", "10")],
    )
    assert is_corrupt_book(book) is False


# ---------------------------------------------------------------------------
# Edge cases — one-sided and empty books
# ---------------------------------------------------------------------------


def test_empty_book() -> None:
    """Completely empty book (no bids, no asks) → False (not the #180 pattern)."""
    book = make_book("t", bids=[], asks=[])
    assert is_corrupt_book(book) is False


def test_only_bids_no_asks() -> None:
    """One-sided book with only bids → False."""
    book = make_book("t", bids=[("0.01", "100")], asks=[])
    assert is_corrupt_book(book) is False


def test_only_asks_no_bids() -> None:
    """One-sided book with only asks → False."""
    book = make_book("t", bids=[], asks=[("0.99", "100")])
    assert is_corrupt_book(book) is False


def test_bids_all_zero_size_effectively_one_sided() -> None:
    """All bid levels have size=0 (phantom) → effectively no bids → False."""
    book = make_book(
        "t",
        bids=[("0.01", "0"), ("0.50", "0")],  # all phantom
        asks=[("0.99", "100")],
    )
    assert is_corrupt_book(book) is False


def test_asks_all_zero_size_effectively_one_sided() -> None:
    """All ask levels have size=0 → effectively no asks → False."""
    book = make_book(
        "t",
        bids=[("0.01", "100")],
        asks=[("0.99", "0"), ("0.50", "0")],  # all phantom
    )
    assert is_corrupt_book(book) is False


# ---------------------------------------------------------------------------
# Detector-level integration: complement suppression
# ---------------------------------------------------------------------------


def test_complement_skips_corrupt_yes_book() -> None:
    """ComplementDetector SKIPS a market when the YES book is corrupt (#180 pattern).

    Scenario construction: YES book is corrupt (bid=0.01, ask=0.99).
    NO book is real and very cheap (ask=0.003).  The apparent complement-under math
    gives YES ask (0.99) + NO ask (0.003) = 0.993 < 1 → gross = 0.007 > 0, which
    WOULD be emitted without the corrupt-book guard.  With the guard, the detector
    must skip this market entirely.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", bids=[("0.01", "100")], asks=[("0.99", "100")]),  # corrupt
            "N": make_book("N", bids=[("0.002", "100")], asks=[("0.003", "100")]),  # real
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


def test_complement_skips_corrupt_no_book() -> None:
    """ComplementDetector SKIPS a market when the NO book is corrupt (#180 pattern)."""
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", bids=[("0.002", "100")], asks=[("0.003", "100")]),  # real
            "N": make_book("N", bids=[("0.01", "100")], asks=[("0.99", "100")]),  # corrupt
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


def test_complement_emits_on_good_books_not_gated() -> None:
    """Control: ComplementDetector still emits on non-corrupt books with a real under edge.

    Both books are legitimate (bid=0.30, ask=0.40 for YES; bid=0.40, ask=0.50 for NO).
    The guard must not block real opportunities — it is a false-negative-only filter.
    """
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", bids=[("0.30", "100")], asks=[("0.40", "100")]),
            "N": make_book("N", bids=[("0.40", "100")], asks=[("0.50", "100")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    assert len(opps) == 1, "expected one under-arb on a legitimate 0.40+0.50=0.90 book"
    assert opps[0].gross_profit == Decimal("0.10")
