"""Offline tests for SqliteStore (in-memory; no network, no file I/O)."""

from __future__ import annotations

from decimal import Decimal

from polyarb.models import DetectorKind, Opportunity
from polyarb.sinks.store import SqliteStore

ZERO = Decimal(0)


def _opp(description: str = "test") -> Opportunity:
    return Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description=description,
        condition_ids=["0x1"],
        legs=[],
        cost=Decimal("0.90"),
        gross_profit=Decimal("0.10"),
        fees=ZERO,
        gas=ZERO,
        net_profit=Decimal("0.10"),
        net_profit_bps=Decimal("1111"),
        executable_size=Decimal("100"),
        realizes="instant",
    )


def test_fresh_store_count_is_zero() -> None:
    with SqliteStore() as store:
        assert store.count() == 0


def test_record_then_count() -> None:
    with SqliteStore() as store:
        store.record(_opp())
        assert store.count() == 1


def test_round_trip_fidelity() -> None:
    opp = _opp("round-trip")
    with SqliteStore() as store:
        store.record(opp)
        recalled = store.recent(limit=1)
    assert len(recalled) == 1
    assert recalled[0].model_dump() == opp.model_dump()


def test_recent_returns_newest_first() -> None:
    with SqliteStore() as store:
        store.record(_opp("first"))
        store.record(_opp("second"))
        results = store.recent()
    # newest (second) should come first
    assert results[0].description == "second"
    assert results[1].description == "first"


def test_recent_respects_limit() -> None:
    with SqliteStore() as store:
        store.record(_opp("a"))
        store.record(_opp("b"))
        store.record(_opp("c"))
        results = store.recent(limit=1)
    assert len(results) == 1
    assert results[0].description == "c"  # newest


def test_context_manager_closes() -> None:
    # After __exit__, further operations on the raw conn should raise; we just verify
    # that the context manager protocol works without error during normal use.
    with SqliteStore() as store:
        store.record(_opp())
        assert store.count() == 1
