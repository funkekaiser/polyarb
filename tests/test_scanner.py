"""End-to-end scanner test — fully offline via httpx.MockTransport, in-memory SQLite.

Exercises discover → fetch books → detect → filter → rank → persist without any network.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx

from polyarb.clients.clob import ClobClient
from polyarb.clients.gamma import GammaClient
from polyarb.config import Settings
from polyarb.engine.scanner import Scanner
from polyarb.models import DetectorKind
from polyarb.sinks.notify import NullNotifier
from polyarb.sinks.store import SqliteStore


def _market(condition_id: str, yes: str, no: str) -> dict:
    return {
        "id": "10",
        "conditionId": condition_id,
        "question": "Will X happen?",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([yes, no]),
        "active": True,
        "closed": False,
        "negRisk": False,
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


def _book(asset_id: str, *, ask: str, bid: str) -> dict:
    return {
        "market": "0xC",
        "asset_id": asset_id,
        "timestamp": "1",
        "bids": [{"price": bid, "size": "500"}],
        "asks": [{"price": ask, "size": "500"}],
        "neg_risk": False,
        "tick_size": "0.01",
        "min_order_size": "5",
    }


def _transport(books: dict[str, dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/events":
            return httpx.Response(200, json=EVENTS)
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


def _run_scan(books: dict[str, dict]) -> tuple[list, SqliteStore]:
    transport = _transport(books)
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
