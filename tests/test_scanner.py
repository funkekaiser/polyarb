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
from polyarb.models import DetectorKind, OrderBook
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


# §5 partial: 3-outcome event where leg 2 is unbuyable (no book) but carries a cached bestAsk,
# and T = 0.30*3 = 0.90 < 1 (an unfillable structural arb).
PARTIAL_EVENT = {
    "id": "3",
    "title": "Partial event",
    "negRisk": True,
    "active": True,
    "closed": False,
    "markets": [
        _negrisk_market("0xP0", "PY0", "PN0"),
        _negrisk_market("0xP1", "PY1", "PN1"),
        {**_negrisk_market("0xP2", "PY2", "PN2"), "bestAsk": "0.30"},  # unbuyable: bestAsk, no book
    ],
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


def _run_scan(
    books: dict[str, dict],
    events: list[dict] | None = None,
    settings: Settings | None = None,
) -> tuple[list, SqliteStore]:
    transport = _transport(books, events)
    store = SqliteStore(":memory:")

    async def go() -> list:
        async with (
            httpx.AsyncClient(transport=transport) as gamma_http,
            httpx.AsyncClient(transport=transport) as clob_http,
        ):
            scanner = Scanner(
                settings or _settings(),
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


# --- streaming scan path (phase 3: trigger off cache + REST-confirm before emit) ---


def _streaming_settings() -> Settings:
    return Settings(
        streaming_enabled=True,
        min_profit_bps=Decimal(1),
        min_notional_usdc=Decimal(1),
        dedupe_cooldown_seconds=0.0,
        max_markets_per_scan=10,
        event_discovery_limit=10,
    )


def _run_streaming_scan(
    cache_books: list[OrderBook], rest_books: dict[str, dict]
) -> tuple[list, SqliteStore]:
    """Seed the cache with ``cache_books``, serve ``rest_books`` over REST; run ONE stream pass."""
    transport = _transport(rest_books)
    store = SqliteStore(":memory:")

    async def go() -> list:
        async with (
            httpx.AsyncClient(transport=transport) as gamma_http,
            httpx.AsyncClient(transport=transport) as clob_http,
        ):
            scanner = Scanner(
                _streaming_settings(),
                gamma=GammaClient(client=gamma_http),
                clob=ClobClient(client=clob_http),
                store=store,
                notifier=NullNotifier(),
            )
            assert scanner._cache is not None
            for ob in cache_books:
                scanner._cache.seed(ob)
            return await scanner._scan_streaming_once()

    return asyncio.run(go()), store


def test_streaming_scan_confirms_and_persists() -> None:
    # Cache shows the under edge AND fresh REST books confirm it → emitted (from fresh books).
    yes = OrderBook.model_validate(_book("Y", ask="0.40", bid="0.30"))
    no = OrderBook.model_validate(_book("N", ask="0.50", bid="0.40"))
    rest = {"Y": _book("Y", ask="0.40", bid="0.30"), "N": _book("N", ask="0.50", bid="0.40")}
    opps, store = _run_streaming_scan([yes, no], rest)
    assert len(opps) == 1
    assert opps[0].detector == DetectorKind.COMPLEMENT
    assert store.count() == 1
    store.close()


def test_streaming_scan_drops_unconfirmed_candidate() -> None:
    # The committee guardrail: a phantom cache edge that the fresh REST book no longer shows must
    # NOT be emitted. Cache says YES 0.40 + NO 0.50 < 1, but REST says YES ask moved to 0.70.
    yes = OrderBook.model_validate(_book("Y", ask="0.40", bid="0.30"))
    no = OrderBook.model_validate(_book("N", ask="0.50", bid="0.40"))
    rest = {"Y": _book("Y", ask="0.70", bid="0.30"), "N": _book("N", ask="0.50", bid="0.40")}
    opps, store = _run_streaming_scan([yes, no], rest)
    assert opps == []
    assert store.count() == 0
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


def test_scanner_partial_basket_opt_in() -> None:
    # §5 is OFF by default → no partial basket even when the unfillable-arb setup is present;
    # enabling the flag makes the scanner emit the directional partial. Only PY0/PY1 have books
    # (PY2 unbuyable, NO books absent) so neither the YES basket nor the dual fires.
    books = {f"PY{i}": _book(f"PY{i}", ask="0.30", bid="0.20") for i in range(2)}

    off, store = _run_scan(books, events=[PARTIAL_EVENT])
    assert all(o.detector != DetectorKind.PARTIAL_BASKET for o in off)
    store.close()

    on_settings = Settings(
        min_profit_bps=Decimal(1),
        min_notional_usdc=Decimal(1),
        dedupe_cooldown_seconds=0.0,
        max_markets_per_scan=10,
        event_discovery_limit=10,
        enable_partial_baskets=True,
    )
    on, store2 = _run_scan(books, events=[PARTIAL_EVENT], settings=on_settings)
    partials = [o for o in on if o.detector == DetectorKind.PARTIAL_BASKET]
    assert len(partials) == 1
    assert partials[0].resolution_risk == "directional"  # ranks below every structural arb
    # Mutual exclusion: the structural YES basket never co-fires (a leg is unbuyable).
    assert DetectorKind.NEGRISK_BASKET not in {o.detector for o in on}
    store2.close()


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
    )
    held = instant.model_copy(update={"realizes": "resolution"})

    assert resolution_risk_for(instant, by_condition) == ResolutionRisk.OBJECTIVE
    assert resolution_risk_for(held, by_condition) == ResolutionRisk.ELEVATED


def test_active_dispute_excludes_held_arb_not_instant() -> None:
    """C1: a held arb spanning a market with an active UMA dispute is tagged AT_RISK (excluded
    by the default filter); an instant arb on the same market stays OBJECTIVE (dispute is
    irrelevant — it realizes before resolution)."""
    from polyarb.detectors.base import Profit, make_opportunity
    from polyarb.engine.scanner import resolution_risk_for
    from polyarb.resolution.risk import ResolutionRisk
    from tests.helpers import make_market

    disputed = make_market("0xD", yes="y", no="n").model_copy(
        update={"fee_type": "crypto_fees_v2", "uma_resolution_statuses": ["disputed"]}
    )
    by_condition = {"0xD": disputed}
    profit = Profit(cost=Decimal("0.90"), gross_profit=Decimal("0.10"), fees=Decimal(0))
    held = make_opportunity(
        detector=DetectorKind.NEGRISK_BASKET,
        description="b",
        condition_ids=["0xD"],
        legs=[],
        profit=profit,
        executable_size=Decimal(1),
        realizes="resolution",
    )
    instant = held.model_copy(update={"realizes": "instant"})

    assert resolution_risk_for(held, by_condition) == ResolutionRisk.AT_RISK
    assert resolution_risk_for(instant, by_condition) == ResolutionRisk.OBJECTIVE


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


def test_run_closes_notifier_on_shutdown() -> None:
    # Engine bug: run() must aclose() the notifier on exit (releases the webhook httpx client).
    class _SpyNotifier:
        def __init__(self) -> None:
            self.closed = False

        async def notify(self, opp: object) -> None:
            pass

        async def aclose(self) -> None:
            self.closed = True

    spy = _SpyNotifier()
    transport = _transport({})  # no books → no opps; we only care that aclose runs

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as c:
            scanner = Scanner(
                _settings(),
                gamma=GammaClient(client=c),
                clob=ClobClient(client=c),
                store=SqliteStore(":memory:"),
                notifier=spy,  # type: ignore[arg-type]
            )
            await scanner.run(passes=1)

    asyncio.run(go())
    assert spy.closed is True


def test_emitted_counts_only_successes() -> None:
    # Engine bug: a store.record failure must NOT inflate the emitted counter (it counts
    # successes, not len(kept)) — accurate monitoring exactly when the system is degraded.
    class _FailingStore:
        def record(self, opp: object) -> None:
            raise RuntimeError("disk full")

        def close(self) -> None:
            pass

    books = {"Y": _book("Y", ask="0.40", bid="0.30"), "N": _book("N", ask="0.50", bid="0.40")}
    transport = _transport(books)

    async def go() -> tuple[Scanner, list]:
        async with httpx.AsyncClient(transport=transport) as c:
            scanner = Scanner(
                _settings(),
                gamma=GammaClient(client=c),
                clob=ClobClient(client=c),
                store=_FailingStore(),  # type: ignore[arg-type]
                notifier=NullNotifier(),
            )
            kept = await scanner.scan_once()
            return scanner, kept

    scanner, kept = asyncio.run(go())
    assert len(kept) == 1  # the complement arb is still detected and returned
    assert scanner._totals["emitted"] == 0  # but nothing was successfully stored/emitted


# ---------------------------------------------------------------------------
# Dynamic gas integration (B2') — Scanner._resolve_gas
# ---------------------------------------------------------------------------


def _gas_transport(*, gas_status: int = 200, cg_status: int = 200) -> httpx.MockTransport:
    """Route gas-station / CoinGecko hosts for the injected GasClient; 404 elsewhere."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "gasstation.polygon.technology" in host:
            return httpx.Response(gas_status, json={"standard": {"maxFee": 30.0}})
        if "coingecko.com" in host:
            return httpx.Response(cg_status, json={"polygon-ecosystem-token": {"usd": 0.10}})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _scanner_with_gas(http: httpx.AsyncClient, gas_client, settings: Settings | None = None):
    """A Scanner with dummy gamma/clob over ``http`` (unused by _resolve_gas) + injected gas
    client. Returns ``(scanner, store)`` so the caller can close the store; ``http`` is owned
    and closed by the caller's ``async with``."""
    store = SqliteStore(":memory:")
    scanner = Scanner(
        settings or _settings(),
        gamma=GammaClient(client=http),
        clob=ClobClient(client=http),
        store=store,
        notifier=NullNotifier(),
        gas_client=gas_client,
    )
    return scanner, store


def _dummy_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})))


def test_resolve_gas_uses_live_oracle_when_client_injected() -> None:
    # An injected GasClient makes _resolve_gas return live-derived costs, NOT the static config.
    from polyarb.clients.gas import FIXED_GAS_UNITS, PER_LEG_GAS_UNITS, GasClient

    def expected(units: int) -> Decimal:
        return Decimal(units) * Decimal("30.0") * Decimal("1e-9") * Decimal("0.10")

    async def go() -> tuple[Decimal, Decimal]:
        async with (
            _dummy_http() as http,
            httpx.AsyncClient(transport=_gas_transport()) as gas_http,
        ):
            scanner, store = _scanner_with_gas(http, GasClient(client=gas_http))
            try:
                return await scanner._resolve_gas()
            finally:
                store.close()

    fixed, per_leg = asyncio.run(go())
    assert fixed == expected(FIXED_GAS_UNITS)
    assert per_leg == expected(PER_LEG_GAS_UNITS)


def test_resolve_gas_falls_back_to_static_on_oracle_failure() -> None:
    # A live-oracle failure (HTTP 500) must NOT abort: _resolve_gas returns the static config gas.
    from polyarb.clients.gas import GasClient

    settings = _settings().model_copy(
        update={"gas_estimate": Decimal("0.02"), "gas_per_leg_estimate": Decimal("0.05")}
    )

    async def go() -> tuple[Decimal, Decimal]:
        async with (
            _dummy_http() as http,
            httpx.AsyncClient(transport=_gas_transport(gas_status=500)) as gas_http,
        ):
            scanner, store = _scanner_with_gas(http, GasClient(client=gas_http), settings)
            try:
                return await scanner._resolve_gas()
            finally:
                store.close()

    fixed, per_leg = asyncio.run(go())
    assert fixed == Decimal("0.02")
    assert per_leg == Decimal("0.05")


def test_resolve_gas_static_when_dynamic_disabled() -> None:
    # Default path: no injected client and use_dynamic_gas=False → _gas_client is None,
    # _resolve_gas returns the static config gas and never constructs a live client.
    settings = _settings().model_copy(
        update={"gas_estimate": Decimal("0.01"), "gas_per_leg_estimate": Decimal("0.03")}
    )

    async def go() -> tuple[Decimal, Decimal]:
        async with _dummy_http() as http:
            scanner, store = _scanner_with_gas(http, None, settings)
            assert scanner._gas_client is None
            try:
                return await scanner._resolve_gas()
            finally:
                store.close()

    fixed, per_leg = asyncio.run(go())
    assert fixed == Decimal("0.01")
    assert per_leg == Decimal("0.03")


def test_use_dynamic_gas_flag_constructs_client() -> None:
    # When use_dynamic_gas=True and no client is injected, the Scanner builds its own GasClient.
    from polyarb.clients.gas import GasClient

    settings = _settings().model_copy(update={"use_dynamic_gas": True})

    async def go() -> bool:
        async with _dummy_http() as http:
            scanner, store = _scanner_with_gas(http, None, settings)
            try:
                is_gas = isinstance(scanner._gas_client, GasClient)
                # Close the Scanner-owned gas client so its httpx pool doesn't leak.
                await scanner._gas_client.aclose()
                return is_gas
            finally:
                store.close()

    assert asyncio.run(go()) is True


def test_run_closes_gas_client_on_shutdown() -> None:
    # The run() finally block must close the gas client on every exit path (mirrors the
    # notifier-close guarantee). A spy records whether aclose() was called.
    class _SpyGas:
        def __init__(self) -> None:
            self.closed = False

        async def gas_costs(self) -> tuple[Decimal, Decimal]:
            return Decimal("0.02"), Decimal("0.05")

        async def aclose(self) -> None:
            self.closed = True

    spy = _SpyGas()
    books = {"Y": _book("Y", ask="0.40", bid="0.30"), "N": _book("N", ask="0.50", bid="0.40")}
    transport = _transport(books)

    async def go() -> _SpyGas:
        async with httpx.AsyncClient(transport=transport) as c:
            scanner = Scanner(
                _settings(),
                gamma=GammaClient(client=c),
                clob=ClobClient(client=c),
                store=SqliteStore(":memory:"),
                notifier=NullNotifier(),
                gas_client=spy,  # type: ignore[arg-type]
            )
            await scanner.run(passes=1)
            return spy

    assert asyncio.run(go()).closed is True


# ---------------------------------------------------------------------------
# D7-heartbeat — file write and healthcheck logic
# ---------------------------------------------------------------------------


def test_heartbeat_file_written_after_run_pass(tmp_path) -> None:
    """Scanner.run(passes=1) with a heartbeat_path writes a parseable recent timestamp."""
    import time

    hb_file = tmp_path / "polyarb-heartbeat"
    books = {"Y": _book("Y", ask="0.40", bid="0.30"), "N": _book("N", ask="0.50", bid="0.40")}
    transport = _transport(books)
    settings = _settings().model_copy(update={"heartbeat_path": hb_file})

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

    before = time.time()
    asyncio.run(go())
    after = time.time()

    assert hb_file.exists(), "heartbeat file must be created after run(passes=1)"
    raw = hb_file.read_text().strip()
    ts = float(raw)
    assert before - 5 <= ts <= after + 5, f"timestamp {ts} outside [{before}, {after}]"


def test_heartbeat_file_written_after_error_pass(tmp_path) -> None:
    """Heartbeat is written even when scan_once raises — a crashing loop is still alive."""
    import time

    hb_file = tmp_path / "polyarb-heartbeat"

    class _BoomGamma:
        """Gamma that always raises so scan_once hits the except branch."""

        async def get_events(self, **_: object) -> list:
            raise RuntimeError("boom")

        async def __aenter__(self) -> _BoomGamma:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

    settings = _settings().model_copy(update={"heartbeat_path": hb_file})

    async def go() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
        ) as c:
            scanner = Scanner(
                settings,
                gamma=_BoomGamma(),  # type: ignore[arg-type]
                clob=ClobClient(client=c),
                store=SqliteStore(":memory:"),
                notifier=NullNotifier(),
            )
            await scanner.run(passes=1)

    before = time.time()
    asyncio.run(go())
    after = time.time()

    assert hb_file.exists(), "heartbeat must be written even after a failing pass"
    ts = float(hb_file.read_text().strip())
    assert before - 5 <= ts <= after + 5


def test_heartbeat_disabled_by_default(tmp_path) -> None:
    """With heartbeat_path=None (default) no file is created — existing tests unaffected."""
    books = {"Y": _book("Y", ask="0.40", bid="0.30"), "N": _book("N", ask="0.50", bid="0.40")}
    transport = _transport(books)
    # Default settings: heartbeat_path is None
    settings = _settings()
    assert settings.heartbeat_path is None

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
    # No heartbeat file should exist anywhere in tmp_path
    assert list(tmp_path.iterdir()) == []


def test_healthcheck_ok_fresh_timestamp(tmp_path) -> None:
    """healthcheck exits 0 when the heartbeat file is fresh."""
    import time

    from typer.testing import CliRunner

    from polyarb.cli import app

    hb_file = tmp_path / "polyarb-heartbeat"
    hb_file.write_text(repr(time.time()))

    runner = CliRunner()
    result = runner.invoke(app, ["healthcheck"], env={"HEARTBEAT_PATH": str(hb_file)})
    assert result.exit_code == 0, f"expected exit 0, got {result.exit_code}: {result.output}"
    assert "ok" in result.output


def test_healthcheck_fail_stale_timestamp(tmp_path) -> None:
    """healthcheck exits non-zero when the heartbeat is older than the freshness window."""
    from typer.testing import CliRunner

    from polyarb.cli import app

    hb_file = tmp_path / "polyarb-heartbeat"
    # Write a timestamp far in the past (well beyond the 120s floor)
    hb_file.write_text(repr(0.0))  # Unix epoch — always stale

    runner = CliRunner()
    result = runner.invoke(app, ["healthcheck"], env={"HEARTBEAT_PATH": str(hb_file)})
    assert result.exit_code != 0, "expected non-zero exit for a stale heartbeat"


def test_healthcheck_fail_missing_file(tmp_path) -> None:
    """healthcheck exits non-zero when the heartbeat file does not exist."""
    from typer.testing import CliRunner

    from polyarb.cli import app

    missing = tmp_path / "no-such-heartbeat"

    runner = CliRunner()
    result = runner.invoke(app, ["healthcheck"], env={"HEARTBEAT_PATH": str(missing)})
    assert result.exit_code != 0, "expected non-zero exit when file is missing"


def test_healthcheck_fail_no_path_configured(monkeypatch) -> None:
    """healthcheck exits non-zero when HEARTBEAT_PATH is not set in the environment."""
    from typer.testing import CliRunner

    from polyarb.cli import app

    # Remove HEARTBEAT_PATH from the environment so Settings.heartbeat_path is None.
    monkeypatch.delenv("HEARTBEAT_PATH", raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["healthcheck"])
    assert result.exit_code != 0, "expected non-zero exit when path is not configured"


def test_scan_once_survives_gas_oracle_failure_end_to_end() -> None:
    # End-to-end: a failing gas oracle must NOT abort scan_once — the complement arb is still
    # detected using the static config gas (gas off ⇒ net 0.10 unchanged).
    from polyarb.clients.gas import GasClient

    books = {"Y": _book("Y", ask="0.40", bid="0.30"), "N": _book("N", ask="0.50", bid="0.40")}
    transport = _transport(books)

    async def go() -> list:
        async with (
            httpx.AsyncClient(transport=transport) as c,
            httpx.AsyncClient(transport=_gas_transport(gas_status=500)) as gas_http,
        ):
            scanner = Scanner(
                _settings(),  # default gas_estimate/gas_per_leg are 0.02/0.05 but min_notional=1
                gamma=GammaClient(client=c),
                clob=ClobClient(client=c),
                store=SqliteStore(":memory:"),
                notifier=NullNotifier(),
                gas_client=GasClient(client=gas_http),
            )
            return await scanner.scan_once()

    opps = asyncio.run(go())
    assert len(opps) == 1
    assert opps[0].detector == DetectorKind.COMPLEMENT
