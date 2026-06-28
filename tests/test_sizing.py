"""Executable-size / book-depth tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from polyarb.pricing.sizing import depth_at_or_better, executable_size
from tests.helpers import make_book


def test_depth_buy_sums_asks_at_or_below_limit() -> None:
    book = make_book(asks=[("0.40", "10"), ("0.45", "5"), ("0.50", "20")])
    assert depth_at_or_better(book, "buy", Decimal("0.45")) == Decimal(15)


def test_depth_sell_sums_bids_at_or_above_limit() -> None:
    book = make_book(bids=[("0.60", "10"), ("0.55", "5"), ("0.50", "20")])
    assert depth_at_or_better(book, "sell", Decimal("0.55")) == Decimal(15)


def test_executable_size_is_thinnest_leg() -> None:
    assert executable_size([Decimal(10), Decimal(3), Decimal(7)]) == Decimal(3)
    assert executable_size([]) == Decimal(0)


def test_invalid_side_raises() -> None:
    with pytest.raises(ValueError):
        depth_at_or_better(make_book(), "hold", Decimal("0.5"))
