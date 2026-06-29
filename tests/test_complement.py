"""Complement detector — unit tests for under/over and no-false-positive."""

from __future__ import annotations

from decimal import Decimal

from polyarb.detectors.base import Snapshot
from polyarb.detectors.complement import ComplementDetector, over_profit, under_profit
from tests.helpers import make_book, make_market

ZERO = Decimal(0)


def test_under_profit_formula() -> None:
    p = under_profit(Decimal("0.40"), Decimal("0.50"), ZERO)
    assert p.cost == Decimal("0.90")
    assert p.gross_profit == Decimal("0.10")
    assert p.net_profit == Decimal("0.10")


def test_over_profit_formula() -> None:
    p = over_profit(Decimal("0.60"), Decimal("0.55"), ZERO)
    assert p.cost == Decimal(1)
    assert p.gross_profit == Decimal("0.15")


def test_detector_emits_under() -> None:
    market = make_market(yes="Y", no="N", fee_rate=None)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.40", "100")], bids=[("0.30", "100")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.40", "100")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.net_profit == Decimal("0.10")
    assert opp.executable_size == Decimal(100)
    assert opp.realizes == "instant"
    assert {leg.side for leg in opp.legs} == {"buy"}


def test_detector_emits_over() -> None:
    market = make_market(yes="Y", no="N")
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.95", "100")], bids=[("0.60", "100")]),
            "N": make_book("N", asks=[("0.95", "100")], bids=[("0.55", "100")]),
        },
    )
    opps = list(ComplementDetector().detect(snap))
    assert len(opps) == 1
    assert opps[0].gross_profit == Decimal("0.15")
    assert {leg.side for leg in opps[0].legs} == {"sell"}


def test_detector_no_opportunity_when_no_arb() -> None:
    market = make_market(yes="Y", no="N")
    snap = Snapshot(
        markets=[market],
        books={
            # asks sum 1.10 (no under); bids sum 0.85 (no over)
            "Y": make_book("Y", asks=[("0.55", "100")], bids=[("0.45", "100")]),
            "N": make_book("N", asks=[("0.55", "100")], bids=[("0.40", "100")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []


def test_fees_can_erase_a_thin_edge() -> None:
    # Tiny gross edge (sum 0.99) wiped out by a 7% fee near 0.5 → no emission.
    market = make_market(yes="Y", no="N", fee_rate=0.07)
    snap = Snapshot(
        markets=[market],
        books={
            "Y": make_book("Y", asks=[("0.49", "100")], bids=[("0.10", "100")]),
            "N": make_book("N", asks=[("0.50", "100")], bids=[("0.10", "100")]),
        },
    )
    assert list(ComplementDetector().detect(snap)) == []
