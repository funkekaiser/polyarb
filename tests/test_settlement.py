"""E1-b/c — realized-outcome settlement math + read-only poller (offline)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from polyarb.engine.settlement import poll_settlements, settle, token_resolution_map
from polyarb.models import DetectorKind, Leg, Market, Opportunity
from polyarb.sinks.store import SqliteStore

ZERO = Decimal(0)


def _leg(token_id: str, price: str, side: str = "buy", size: str = "100") -> Leg:
    return Leg(token_id=token_id, side=side, price=Decimal(price), size=Decimal(size))


def _opp(
    legs: list[Leg], *, gas: str = "0", detector: DetectorKind = DetectorKind.COMPLEMENT
) -> Opportunity:
    return Opportunity(
        detector=detector,
        description="settle-test",
        legs=legs,
        cost=Decimal("0.90"),
        gross_profit=Decimal("0.10"),
        fees=ZERO,
        gas=Decimal(gas),
        net_profit=Decimal("0.10"),
        net_profit_bps=Decimal("1111"),
        executable_size=Decimal("100"),
        realizes="resolution",
    )


def test_clean_resolution_complement_pays_out() -> None:
    # Buy YES(0.45) + NO(0.45), size 100. YES wins (1), NO loses (0).
    opp = _opp([_leg("yA", "0.45"), _leg("nA", "0.45")])
    result = settle(opp, {"yA": Decimal(1), "nA": Decimal(0)})
    assert result is not None
    assert result.status == "resolved"
    assert result.realized_payoff == Decimal("100")  # 100·1 + 100·0
    assert result.realized_pnl == Decimal("10")  # 100·(1-0.45) + 100·(0-0.45)


def test_complement_is_void_immune() -> None:
    # A single market voids → BOTH its YES and NO settle 0.5. The complement lock still pays
    # 100·0.5 + 100·0.5 = 100, so P&L is unchanged — matches the committee's void analysis.
    opp = _opp([_leg("yA", "0.45"), _leg("nA", "0.45")])
    result = settle(opp, {"yA": Decimal("0.5"), "nA": Decimal("0.5")})
    assert result is not None
    assert result.status == "void"  # flagged, but...
    assert result.realized_pnl == Decimal("10")  # ...still profitable


def test_dependency_void_causes_a_loss() -> None:
    # A thin dependency lock (YES_B + NO_A, cost 0.60) where B voids (0.5) and A occurs
    # (NO_A → 0): payoff 50·0.5 + 50·0 = 25, P&L = 50·(0.5-0.30) + 50·(0-0.30) = -5.
    # This is exactly the loss the A2-void OBJECTIVE-source gate now prevents.
    opp = _opp(
        [_leg("yB", "0.30", size="50"), _leg("nA", "0.30", size="50")],
        detector=DetectorKind.DEPENDENCY,
    )
    result = settle(opp, {"yB": Decimal("0.5"), "nA": Decimal(0)})
    assert result is not None
    assert result.status == "void"
    assert result.realized_pnl == Decimal("-5")


def test_gas_is_subtracted() -> None:
    opp = _opp([_leg("yA", "0.45"), _leg("nA", "0.45")], gas="3")
    result = settle(opp, {"yA": Decimal(1), "nA": Decimal(0)})
    assert result is not None
    assert result.realized_pnl == Decimal("7")  # 10 - 3 gas


def test_sell_leg_pnl_sign() -> None:
    # A short: received 0.60 entry, token settles 0 → keep the premium. P&L = 100·(0.60-0) = 60.
    opp = _opp([_leg("yA", "0.60", side="sell")])
    result = settle(opp, {"yA": Decimal(0)})
    assert result is not None
    assert result.realized_pnl == Decimal("60")


def test_pending_when_a_leg_is_unresolved() -> None:
    opp = _opp([_leg("yA", "0.45"), _leg("nA", "0.45")])
    assert settle(opp, {"yA": Decimal(1)}) is None  # nA missing → still pending


def test_no_legs_returns_none() -> None:
    assert settle(_opp([]), {}) is None


def test_token_resolution_map_skips_open_markets() -> None:
    closed = Market(
        id="1",
        condition_id="0xA",
        question="Q?",
        outcomes=["Yes", "No"],
        clob_token_ids=["yA", "nA"],
        outcome_prices=[Decimal(1), Decimal(0)],
        closed=True,
    )
    open_market = Market(
        id="2",
        condition_id="0xB",
        question="Q?",
        outcomes=["Yes", "No"],
        clob_token_ids=["yB", "nB"],
        outcome_prices=[Decimal("0.4"), Decimal("0.6")],  # live, not a settlement
        closed=False,
    )
    resolved = token_resolution_map([closed, open_market])
    assert resolved == {"yA": Decimal(1), "nA": Decimal(0)}
    assert "yB" not in resolved


# ---------------------------------------------------------------------------
# E1-c — read-only settlement poller over the ledger
# ---------------------------------------------------------------------------


class _FakeResolver:
    """Returns the closed markets whose condition_id is asked for (a fake GammaClient)."""

    def __init__(self, markets: list[Market]) -> None:
        self._markets = markets
        self.asked: list[str] = []

    async def resolved_markets(self, condition_ids: list[str]) -> list[Market]:
        self.asked = condition_ids
        return [m for m in self._markets if m.condition_id in condition_ids]


def _resolved_market(condition_id: str, tokens: list[str], prices: list[str]) -> Market:
    return Market(
        id=condition_id,
        condition_id=condition_id,
        question="Q?",
        outcomes=["Yes", "No"],
        clob_token_ids=tokens,
        outcome_prices=[Decimal(p) for p in prices],
        closed=True,
    )


def test_poll_settles_a_resolved_event() -> None:
    opp = _opp([_leg("yA", "0.45"), _leg("nA", "0.45")])
    opp.condition_ids = ["0xA"]
    resolver = _FakeResolver([_resolved_market("0xA", ["yA", "nA"], ["1", "0"])])
    with SqliteStore() as store:
        store.record(opp)
        run = asyncio.run(poll_settlements(store, resolver))
        assert (run.checked, run.settled, run.void, run.still_pending) == (1, 1, 0, 0)
        assert store.pending_events() == []  # settled → cleared from pending


def test_poll_flags_a_void_event() -> None:
    opp = _opp([_leg("yA", "0.45"), _leg("nA", "0.45")])
    opp.condition_ids = ["0xA"]
    resolver = _FakeResolver([_resolved_market("0xA", ["yA", "nA"], ["0.5", "0.5"])])
    with SqliteStore() as store:
        store.record(opp)
        run = asyncio.run(poll_settlements(store, resolver))
        assert (run.settled, run.void) == (0, 1)


def test_poll_leaves_unresolved_events_pending() -> None:
    opp = _opp([_leg("yA", "0.45"), _leg("nA", "0.45")])
    opp.condition_ids = ["0xA"]
    resolver = _FakeResolver([])  # nothing resolved yet
    with SqliteStore() as store:
        store.record(opp)
        run = asyncio.run(poll_settlements(store, resolver))
        assert (run.checked, run.still_pending) == (1, 1)
        assert len(store.pending_events()) == 1  # still tracked as pending


def test_poll_empty_ledger_is_a_noop() -> None:
    resolver = _FakeResolver([])
    with SqliteStore() as store:
        run = asyncio.run(poll_settlements(store, resolver))
        assert (run.checked, run.settled, run.void, run.still_pending) == (0, 0, 0, 0)
        assert resolver.asked == []  # never queried Gamma with an empty ledger


def test_scanner_settle_pending_wires_the_poller() -> None:
    # The scanner's slow-cadence hook drives the read-only poller against its own store + gamma.
    from polyarb.config import Settings
    from polyarb.engine.scanner import Scanner

    opp = _opp([_leg("yA", "0.45"), _leg("nA", "0.45")])
    opp.condition_ids = ["0xA"]
    gamma = _FakeResolver([_resolved_market("0xA", ["yA", "nA"], ["1", "0"])])
    store = SqliteStore()
    store.record(opp)
    scanner = Scanner(Settings(), gamma=gamma, clob=None, store=store)  # type: ignore[arg-type]
    try:
        asyncio.run(scanner._settle_pending())
        assert store.pending_events() == []  # the pending event got settled through the scanner
    finally:
        store.close()
