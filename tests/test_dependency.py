"""Dependency detector — A ⇒ B violations and the locked YES_B + NO_A trade."""

from __future__ import annotations

from decimal import Decimal

from polyarb.detectors.base import Snapshot
from polyarb.detectors.dependency import DependencyDetector, dependency_profit
from polyarb.resolution.relations import Relation
from tests.helpers import make_book, make_market

ZERO = Decimal(0)


def test_dependency_profit_formula() -> None:
    # cost = a_yes_B + a_no_A; min payoff 1.
    p = dependency_profit(Decimal("0.30"), Decimal("0.30"), ZERO, ZERO, ZERO)
    assert p.cost == Decimal("0.60")
    assert p.gross_profit == Decimal("0.40")


def _snapshot(a_no_ask: str, b_yes_ask: str) -> Snapshot:
    market_a = make_market("0xA", yes="yA", no="nA")
    market_b = make_market("0xB", yes="yB", no="nB")
    relation = Relation("0xA", "0xB", "A ⇒ B")
    return Snapshot(
        markets=[market_a, market_b],
        relations=[relation],
        books={
            "nA": make_book("nA", asks=[(a_no_ask, "50")]),
            "yB": make_book("yB", asks=[(b_yes_ask, "80")]),
        },
    )


def test_detector_emits_on_violation() -> None:
    snap = _snapshot(a_no_ask="0.30", b_yes_ask="0.30")  # cost 0.60 < 1
    opps = list(DependencyDetector().detect(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.gross_profit == Decimal("0.40")
    assert opp.executable_size == Decimal(50)  # thinnest leg
    assert opp.realizes == "resolution"
    assert opp.condition_ids == ["0xA", "0xB"]


def test_detector_silent_when_no_violation() -> None:
    snap = _snapshot(a_no_ask="0.60", b_yes_ask="0.60")  # cost 1.20 ≥ 1
    assert list(DependencyDetector().detect(snap)) == []


def test_detector_skips_unknown_markets() -> None:
    snap = _snapshot(a_no_ask="0.30", b_yes_ask="0.30")
    snap.relations = [Relation("0xMISSING", "0xB", "dangling")]
    assert list(DependencyDetector().detect(snap)) == []
