"""Smoke tests for the optional Prometheus metrics (no server started)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

from polyarb.engine import metrics


def test_counters_are_defined() -> None:
    assert isinstance(metrics.SCAN_PASSES, Counter)
    assert isinstance(metrics.EMITTED, Counter)
    assert isinstance(metrics.CANDIDATES, Counter)
    assert isinstance(metrics.SCAN_ERRORS, Counter)


def test_increments_do_not_raise() -> None:
    metrics.SCAN_PASSES.inc()
    metrics.EMITTED.inc(2)
    metrics.SCAN_ERRORS.inc()
    metrics.CANDIDATES.labels(detector="complement").inc(3)


def test_last_pass_gauge_is_defined() -> None:
    """D7-heartbeat: LAST_PASS is a Gauge and can be set without error."""
    assert isinstance(metrics.LAST_PASS, Gauge)


def test_last_pass_gauge_set_does_not_raise() -> None:
    """LAST_PASS.set() must not raise (cheap even without a /metrics server)."""
    import time

    metrics.LAST_PASS.set(time.time())  # idempotent, no side-effects


def test_last_pass_set_by_scanner_run(tmp_path) -> None:
    """After Scanner.run(passes=1) the LAST_PASS gauge has been set to a recent timestamp."""
    import asyncio
    import time
    from decimal import Decimal

    import httpx

    from polyarb.clients.clob import ClobClient
    from polyarb.clients.gamma import GammaClient
    from polyarb.config import Settings
    from polyarb.engine.scanner import Scanner
    from polyarb.sinks.notify import NullNotifier
    from polyarb.sinks.store import SqliteStore

    def _book(asset_id: str) -> dict:
        return {
            "market": "0xC",
            "asset_id": asset_id,
            "timestamp": str(int(time.time() * 1000)),
            "bids": [{"price": "0.30", "size": "500"}],
            "asks": [{"price": "0.40", "size": "500"}],
            "neg_risk": False,
            "tick_size": "0.01",
            "min_order_size": "5",
        }

    events = [
        {
            "id": "1",
            "title": "Test",
            "negRisk": False,
            "active": True,
            "closed": False,
            "markets": [
                {
                    "id": "10",
                    "conditionId": "0xM",
                    "question": "Q?",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["Y", "N"]',
                    "active": True,
                    "closed": False,
                    "negRisk": False,
                    "acceptingOrders": True,
                    "orderPriceMinTickSize": 0.01,
                    "orderMinSize": 5,
                }
            ],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            return httpx.Response(200, json=events)
        token = request.url.params.get("token_id", "")
        if token in ("Y", "N"):
            return httpx.Response(200, json=_book(token))
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    settings = Settings(
        min_profit_bps=Decimal(1),
        min_notional_usdc=Decimal(1),
        dedupe_cooldown_seconds=0.0,
        max_markets_per_scan=10,
        event_discovery_limit=10,
    )

    before = time.time()

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as c:
            scanner = Scanner(
                settings,
                gamma=GammaClient(client=c),
                clob=ClobClient(client=c),
                store=SqliteStore(":memory:"),
                notifier=NullNotifier(),
            )
            await scanner.run(passes=1)

    asyncio.run(go())
    after = time.time()

    # Prometheus Gauge._value.get() returns the current value.
    gauge_value = metrics.LAST_PASS._value.get()  # type: ignore[attr-defined]
    assert before - 5 <= gauge_value <= after + 5, (
        f"LAST_PASS gauge value {gauge_value} outside expected range [{before}, {after}]"
    )
