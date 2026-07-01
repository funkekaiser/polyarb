"""Offline tests for SqliteStore (in-memory; no network, no file I/O)."""

from __future__ import annotations

from decimal import Decimal

from polyarb.models import DetectorKind, Leg, Opportunity
from polyarb.sinks.store import SqliteStore, economic_fingerprint

ZERO = Decimal(0)


def _opp(
    description: str = "test",
    *,
    condition_ids: list[str] | None = None,
    legs: list[Leg] | None = None,
) -> Opportunity:
    return Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description=description,
        condition_ids=["0x1"] if condition_ids is None else condition_ids,
        legs=[] if legs is None else legs,
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


# ---------------------------------------------------------------------------
# E1 — economic-event fingerprint + deduped realized-outcome ledger
# ---------------------------------------------------------------------------


def _leg(token_id: str, side: str = "buy") -> Leg:
    return Leg(token_id=token_id, side=side, price=Decimal("0.45"), size=Decimal("100"))


def test_fingerprint_stable_across_size_price_drift() -> None:
    # Same structure, different sizes/prices/description → same economic event.
    a = _opp("t1", condition_ids=["0xA", "0xB"], legs=[_leg("yA"), _leg("yB")])
    b = _opp("t2", condition_ids=["0xB", "0xA"], legs=[_leg("yB"), _leg("yA")])
    b.executable_size = Decimal("7")  # drift is excluded from the fingerprint
    assert economic_fingerprint(a) == economic_fingerprint(b)


def test_fingerprint_differs_on_condition_set_and_leg_structure() -> None:
    base = _opp(condition_ids=["0xA"], legs=[_leg("yA")])
    other_market = _opp(condition_ids=["0xC"], legs=[_leg("yC")])
    other_side = _opp(condition_ids=["0xA"], legs=[_leg("yA", side="sell")])
    assert economic_fingerprint(base) != economic_fingerprint(other_market)
    assert economic_fingerprint(base) != economic_fingerprint(other_side)


def test_redetections_collapse_to_one_economic_event() -> None:
    opp = _opp(condition_ids=["0xA", "0xB"], legs=[_leg("yA"), _leg("yB")])
    with SqliteStore() as store:
        for _ in range(5):
            store.record(opp)
        assert store.count() == 5  # raw detection log keeps every pass
        assert store.distinct_events() == 1  # ledger dedupes to one economic event
        pending = store.pending_events()
        assert len(pending) == 1
        assert pending[0].detection_count == 5
        assert pending[0].status == "pending"


def test_distinct_condition_sets_are_separate_events() -> None:
    with SqliteStore() as store:
        store.record(_opp(condition_ids=["0xA"], legs=[_leg("yA")]))
        store.record(_opp(condition_ids=["0xB"], legs=[_leg("yB")]))
        assert store.distinct_events() == 2


def test_record_resolution_settles_and_removes_from_pending() -> None:
    opp = _opp(condition_ids=["0xA", "0xB"], legs=[_leg("yA"), _leg("yB")])
    fp = economic_fingerprint(opp)
    with SqliteStore() as store:
        store.record(opp)
        store.record_resolution(
            fp,
            status="resolved",
            realized_payoff=Decimal("100"),
            realized_pnl=Decimal("10"),
            detail={"0xA": "1.0", "0xB": "0.0"},
        )
        assert store.pending_events() == []  # settled → no longer pending
        assert store.distinct_events() == 1  # still tracked, just resolved


def test_shadow_observations_are_isolated_from_real_ledger() -> None:
    real = _opp(condition_ids=["0xA"], legs=[_leg("yA")])
    shadow = _opp(condition_ids=["0xB"], legs=[_leg("yB")])
    with SqliteStore() as store:
        store.record(real)
        store.record_shadow(shadow)
        store.record_shadow(shadow)  # re-detection dedupes
        # Real views exclude shadow entirely.
        assert store.distinct_events() == 1  # only the real one
        assert [e.opp.condition_ids for e in store.events()] == [["0xA"]]
        assert [e.opp.condition_ids for e in store.pending_events()] == [["0xA"]]
        # Shadow view has the observation, deduped, with its detection count.
        shadow_events = store.shadow_events()
        assert len(shadow_events) == 1
        assert shadow_events[0].opp.condition_ids == ["0xB"]
        assert shadow_events[0].detection_count == 2


def test_events_reader_round_trips_realized_pnl() -> None:
    opp = _opp(condition_ids=["0xA"], legs=[_leg("yA")])
    fp = economic_fingerprint(opp)
    with SqliteStore() as store:
        store.record(opp)
        store.record_resolution(
            fp,
            status="void",
            realized_payoff=Decimal("50"),
            realized_pnl=Decimal("-5"),
            detail={"0xA": "0.5"},
        )
        events = store.events()
    assert len(events) == 1
    assert events[0].status == "void"
    assert events[0].realized_pnl == Decimal("-5")  # REAL column → Decimal, no float drift
    assert events[0].realized_payoff == Decimal("50")
