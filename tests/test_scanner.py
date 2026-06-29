"""End-to-end scanner test — fully offline via httpx.MockTransport, in-memory SQLite.

Exercises discover → fetch books → detect → filter → rank → persist without any network.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import httpx

from polyarb.clients.clob import ClobClient
from polyarb.clients.gamma import GammaClient
from polyarb.config import Settings
from polyarb.engine.scanner import Scanner
from polyarb.models import DetectorKind
from polyarb.sinks.notify import NullNotifier
from polyarb.sinks.store import SqliteStore


def _market(condition_id: str, yes: str, no: str, *, accepting: bool = True) -> dict:
    return {
        "id": "10",
        "conditionId": condition_id,
        "question": "Will X happen?",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([yes, no]),
        "active": True,
        "closed": False,
        "negRisk": False,
        "acceptingOrders": accepting,
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
    }


EVENTS = [
    {
        "id": "1",
        "title": "Test event",
        "negRisk": False,
        "active": True,
        "closed": False,
        "markets": [_market("0xC", "Y", "N")],
    }
]


def _negrisk_market(condition_id: str, yes: str, no: str) -> dict:
    # OBJECTIVE (feeType carries "sports") so it passes the NO-dual's void gate; fee-free.
    return {
        "id": "20",
        "conditionId": condition_id,
        "question": "Who wins?",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([yes, no]),
        "active": True,
        "closed": False,
        "negRisk": True,
        "acceptingOrders": True,
        "feeType": "sports_fees_v2",
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
    }


NEGRISK_EVENT = {
    "id": "2",
    "title": "Who wins the cup?",
    "negRisk": True,
    "active": True,
    "closed": False,
    "markets": [_negrisk_market(f"0xD{i}", f"DY{i}", f"DN{i}") for i in range(3)],
}


def _book(asset_id: str, *, ask: str, bid: str, age_s: float = 0.0) -> dict:
    # Fresh CLOB book timestamp (epoch ms) so the A3 staleness gate keeps it; ``age_s`` backdates
    # it to exercise the gate's drop path.
    return {
        "market": "0xC",
        "asset_id": asset_id,
        "timestamp": str(int((time.time() - age_s) * 1000)),
        "bids": [{"price": bid, "size": "500"}],
        "asks": [{"price": ask, "size": "500"}],
        "neg_risk": False,
        "tick_size": "0.01",
        "min_order_size": "5",
    }


def _transport(books: dict[str, dict], events: list[dict] | None = None) -> httpx.MockTransport:
    _events = events if events is not None else EVENTS

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/events":
            return httpx.Response(200, json=_events)
        if path == "/book":
            token = request.url.params.get("token_id", "")
            if token in books:
                return httpx.Response(200, json=books[token])
            return httpx.Response(404, json={})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _settings() -> Settings:
    return Settings(
        min_profit_bps=Decimal(1),
        min_notional_usdc=Decimal(1),
        dedupe_cooldown_seconds=0.0,
        max_markets_per_scan=10,
        event_discovery_limit=10,
    )


def _run_scan(books: dict[str, dict], events: list[dict] | None = None) -> tuple[list, SqliteStore]:
    transport = _transport(books, events)
    store = SqliteStore(":memory:")

    async def go() -> list:
        async with (
            httpx.AsyncClient(transport=transport) as gamma_http,
            httpx.AsyncClient(transport=transport) as clob_http,
        ):
            scanner = Scanner(
                _settings(),
                gamma=GammaClient(client=gamma_http),
                clob=ClobClient(client=clob_http),
                store=store,
                notifier=NullNotifier(),
            )
            return await scanner.scan_once()

    return asyncio.run(go()), store


def test_scanner_detects_and_persists_complement_under() -> None:
    # YES ask 0.40 + NO ask 0.50 = 0.90 < 1 → complement under arb.
    books = {"Y": _book("Y", ask="0.40", bid="0.30"), "N": _book("N", ask="0.50", bid="0.40")}
    opps, store = _run_scan(books)
    assert len(opps) == 1
    assert opps[0].detector == DetectorKind.COMPLEMENT
    assert opps[0].net_profit == Decimal("0.10")
    assert opps[0].resolution_risk is not None
    # persisted to SQLite
    assert store.count() == 1
    assert store.recent(10)[0].net_profit == Decimal("0.10")
    store.close()


def test_scanner_drops_stale_books() -> None:
    # A3: same profitable complement, but both books are far older than max_book_age_s (60s) →
    # the staleness gate drops them and nothing is detected (a stale quote can't make an arb).
    books = {
        "Y": _book("Y", ask="0.40", bid="0.30", age_s=3600),
        "N": _book("N", ask="0.50", bid="0.40", age_s=3600),
    }
    opps, store = _run_scan(books)
    assert opps == []
    store.close()


def test_scanner_detects_negrisk_dual() -> None:
    # End-to-end NO-dual: a 3-outcome OBJECTIVE negRisk event with NO asks Σ=1.80 < 2 (M-1) →
    # the scanner fetches NO books and the dual fires. YES asks Σ=1.20 ≥ 1 → no YES basket.
    books = {f"DN{i}": _book(f"DN{i}", ask="0.60", bid="0.50") for i in range(3)}
    books |= {f"DY{i}": _book(f"DY{i}", ask="0.40", bid="0.30") for i in range(3)}
    opps, store = _run_scan(books, events=[NEGRISK_EVENT])
    kinds = {opp.detector for opp in opps}
    assert DetectorKind.NEGRISK_DUAL in kinds
    assert DetectorKind.NEGRISK_BASKET not in kinds  # Σ YES = 1.2, no basket arb
    store.close()


def test_scanner_emits_nothing_when_no_arb() -> None:
    # asks sum 1.10 (no under), bids sum 0.70 (no over) → nothing.
    books = {"Y": _book("Y", ask="0.55", bid="0.35"), "N": _book("N", ask="0.55", bid="0.35")}
    opps, store = _run_scan(books)
    assert opps == []
    assert store.count() == 0
    store.close()


def test_scanner_skips_market_without_book() -> None:
    # Only YES book present; NO returns 404 → cannot evaluate complement.
    books = {"Y": _book("Y", ask="0.40", bid="0.30")}
    opps, store = _run_scan(books)
    assert opps == []
    store.close()


def test_instant_arbs_are_resolution_risk_free() -> None:
    """An instant (complement) arb is tagged OBJECTIVE even on an elevated-category market;
    a held arb takes the market's real risk. (Backlog D4 — instant arbs never reach
    resolution, so resolution risk must not demote or exclude them.)"""
    from polyarb.detectors.base import Profit, make_opportunity
    from polyarb.engine.scanner import resolution_risk_for
    from polyarb.resolution.risk import ResolutionRisk
    from tests.helpers import make_market

    politics = make_market("0xP", yes="y", no="n").model_copy(update={"fee_type": "politics"})
    by_condition = {"0xP": politics}
    profit = Profit(cost=Decimal("0.90"), gross_profit=Decimal("0.10"), fees=Decimal(0))

    instant = make_opportunity(
        detector=DetectorKind.COMPLEMENT,
        description="c",
        condition_ids=["0xP"],
        legs=[],
        profit=profit,
        executable_size=Decimal(1),
        realizes="instant",
        days_to_resolution=None,
    )
    held = instant.model_copy(update={"realizes": "resolution"})

    assert resolution_risk_for(instant, by_condition) == ResolutionRisk.OBJECTIVE
    assert resolution_risk_for(held, by_condition) == ResolutionRisk.ELEVATED


def test_scanner_skips_paused_market() -> None:
    """A market with acceptingOrders=False must not produce any opportunities.

    The books show a clear complement-under arb (YES ask 0.40 + NO ask 0.50 = 0.90),
    but the market is paused, so the scanner must exclude it during discovery.
    """
    paused_events = [
        {
            "id": "2",
            "title": "Paused event",
            "negRisk": False,
            "active": True,
            "closed": False,
            "markets": [_market("0xP", "PY", "PN", accepting=False)],
        }
    ]
    books = {
        "PY": _book("PY", ask="0.40", bid="0.30"),
        "PN": _book("PN", ask="0.50", bid="0.40"),
    }
    opps, store = _run_scan(books, paused_events)
    assert opps == []
    assert store.count() == 0
    store.close()
