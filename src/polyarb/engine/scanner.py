"""The scan loop: discover → read books → detect → filter → rank → emit.

Read-only. It uses only the Gamma (discovery) and CLOB (public book reads) clients — never a
signing client. One ``scan_once`` pass discovers candidate markets, fetches their books,
runs the three detectors, tags resolution risk, filters and ranks, then persists + logs +
(optionally) notifies. ``run`` repeats that on the configured interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import structlog

from polyarb.clients.clob import ClobClient
from polyarb.clients.gamma import GammaClient
from polyarb.clients.gas import GasClient, GasUnavailable
from polyarb.clients.ws import MarketWebSocket
from polyarb.config import Settings
from polyarb.detectors.base import Detector, Snapshot
from polyarb.detectors.complement import ComplementDetector
from polyarb.detectors.dependency import DependencyDetector
from polyarb.detectors.negrisk_basket import NegRiskBasketDetector, NegRiskDualDetector
from polyarb.detectors.partial_basket import PartialBasketDetector
from polyarb.engine import heartbeat, metrics
from polyarb.engine.bookcache import OrderBookCache
from polyarb.engine.confirm import ConfirmContext, confirm_candidate
from polyarb.engine.filters import DedupeCache, OpportunityFilter
from polyarb.engine.ranking import rank
from polyarb.engine.streaming import StreamingBooks
from polyarb.models import DetectorKind, Event, Market, Opportunity, OrderBook
from polyarb.resolution.relations import (
    POLITICS_NESTING,
    SEED_RELATIONS,
    SPORTS_NESTING,
    TAG_REGISTRY,
    MarketTags,
    Relation,
    generate_dag_relations,
    generate_ladder_relations,
)
from polyarb.resolution.risk import ResolutionRisk, aggregate_risk, risk_rank
from polyarb.sinks.notify import Notifier, NullNotifier
from polyarb.sinks.store import OpportunityStore

log = structlog.get_logger("polyarb.scanner")


# ---------------------------------------------------------------------------
# D7-heartbeat helpers (module-level so tests can monkeypatch _now)
# ---------------------------------------------------------------------------


def _now() -> float:
    """Current wall-clock epoch seconds. Module-level so tests can monkeypatch it."""
    return heartbeat.now_epoch()


def _write_heartbeat(path: Path | None) -> None:
    """Atomically write the current epoch seconds to *path* (write-then-rename).

    No-op when *path* is None (default / non-Docker path). Thin wrapper over
    ``heartbeat.write`` that uses this module's ``_now`` so tests can monkeypatch the clock.
    """
    heartbeat.write(path, _now())


def _days_to_resolution(markets: list[Market], now: datetime) -> dict[str, int]:
    """Whole days until each market's end_date (clamped at 0); skips markets without one.

    Gamma usually sends an aware ISO timestamp, but the ``Z`` is optional — a naive end_date
    would otherwise raise ``TypeError`` on subtraction and poison the whole scan pass, so we
    treat naive timestamps as UTC.
    """
    out: dict[str, int] = {}
    for m in markets:
        if m.end_date is None:
            continue
        end = m.end_date if m.end_date.tzinfo is not None else m.end_date.replace(tzinfo=UTC)
        out[m.condition_id] = max((end - now).days, 0)
    return out


def _fresh_books(
    books: dict[str, OrderBook], now: datetime, max_age_s: float
) -> dict[str, OrderBook]:
    """Drop books whose CLOB *last-change* timestamp is older than ``max_age_s`` (vs ``now``).

    ``timestamp_ms`` is the time the book last changed (verified against the live CLOB), not our
    fetch time — so ``now - timestamp_ms`` is a real age. A dropped book makes its detector
    short-circuit (book is None), which can only ever cause a false negative (missed opp), never
    a false positive. This catches grossly-stale / corrupt snapshots; it does NOT distinguish a
    corrupt snapshot from a quiescent-but-valid book (whose resting orders are still
    executable), so it's a conservative net, not a freshness guarantee. Cross-leg skew is
    bounded by roughly ``max_age_s`` (plus per-pass fetch latency, since ``now`` is captured
    once at scan start). ``max_age_s <= 0`` disables. Future-dated books (clock skew) are kept.
    """
    if max_age_s <= 0:
        return books
    now_ms = int(now.timestamp() * 1000)
    cutoff_ms = int(max_age_s * 1000)
    return {t: b for t, b in books.items() if now_ms - b.timestamp_ms <= cutoff_ms}


def resolution_risk_for(opp: Opportunity, markets_by_condition: dict[str, Market]) -> str:
    """Resolution-risk tag for an opportunity.

    Instant arbs (complement merge/split) realize *before* resolution, so how the market
    eventually resolves is irrelevant to the payoff — tag them OBJECTIVE so a market's category
    can't demote or hard-exclude genuinely risk-free money. Held-to-resolution arbs take the
    worst risk across the markets they span.
    """
    if opp.detector == DetectorKind.PARTIAL_BASKET:
        # §5 partial baskets are directional bets, not structural locks → at least DIRECTIONAL so
        # they rank below every structural arb. Take the worse of that and the legs' own
        # resolution risk, so a dispute-prone (AT_RISK) leg still keeps the opp excludable.
        spanned = [markets_by_condition[c] for c in opp.condition_ids if c in markets_by_condition]
        return max(ResolutionRisk.DIRECTIONAL, aggregate_risk(spanned), key=risk_rank)
    if opp.realizes == "instant":
        return ResolutionRisk.OBJECTIVE
    spanned = [markets_by_condition[c] for c in opp.condition_ids if c in markets_by_condition]
    return aggregate_risk(spanned)


class Scanner:
    def __init__(
        self,
        settings: Settings,
        *,
        gamma: GammaClient,
        clob: ClobClient,
        store: OpportunityStore,
        notifier: Notifier | None = None,
        relations: list[Relation] | None = None,
        tags: list[MarketTags] | None = None,
        gas_client: GasClient | None = None,
        ws_factory: Callable[[], MarketWebSocket] | None = None,
    ) -> None:
        self._settings = settings
        self._gamma = gamma
        self._clob = clob
        self._store = store
        self._notifier = notifier or NullNotifier()
        # WS connection factory for the streaming runner (injected as a fake in offline tests; the
        # default builds a real market-channel client at run() time).
        self._ws_factory = ws_factory
        # Live gas oracle (B2'): an injected client wins; else create one only when enabled.
        # When None, the static config gas defaults are used (the default path).
        self._gas_client = gas_client or (GasClient() if settings.use_dynamic_gas else None)
        # Dependency edges = hand-declared relations + auto-generated ladders/DAGs from market
        # tags (docs/RELATIONS.md). With no tags supplied the generators yield nothing, so the
        # dependency detector stays inert until markets are tagged — declared, never inferred.
        declared = list(relations if relations is not None else SEED_RELATIONS)
        market_tags = tags if tags is not None else TAG_REGISTRY
        self._relations = (
            declared
            + generate_ladder_relations(market_tags)
            + generate_dag_relations(market_tags, [*SPORTS_NESTING, *POLITICS_NESTING])
        )
        self._complement = ComplementDetector()
        self._negrisk = NegRiskBasketDetector()
        self._negrisk_dual = NegRiskDualDetector()
        self._partial = PartialBasketDetector()  # §5 — only run when explicitly enabled
        self._dependency = DependencyDetector()
        # Detector registry keyed by kind — used by the streaming REST-confirm gate (R1).
        self._detectors_by_kind: dict[DetectorKind, Detector] = {
            DetectorKind.COMPLEMENT: self._complement,
            DetectorKind.NEGRISK_BASKET: self._negrisk,
            DetectorKind.NEGRISK_DUAL: self._negrisk_dual,
            DetectorKind.PARTIAL_BASKET: self._partial,
            DetectorKind.DEPENDENCY: self._dependency,
        }
        self._dedupe = DedupeCache(settings.dedupe_cooldown_seconds)
        # Websocket streaming (phase 3, opt-in via streaming_enabled). The cache is kept fresh by
        # a StreamingBooks runner started in run(); scan passes then read books from the cache and
        # REST-confirm each candidate before emit (committee verdict — streaming is a trigger).
        self._streaming_enabled = settings.streaming_enabled
        self._cache: OrderBookCache | None = OrderBookCache() if self._streaming_enabled else None
        self._streaming: StreamingBooks | None = None
        # Cumulative metrics across passes (Prometheus /metrics could expose these later).
        self._totals: Counter[str] = Counter()

    async def _resolve_gas(self) -> tuple[Decimal, Decimal]:
        """Per-pass (gas_fixed, gas_per_leg) in USDC: the live oracle when enabled, else the
        static config estimates. A live-oracle failure falls back to the static values (logged)
        and never aborts a pass."""
        if self._gas_client is not None:
            try:
                return await self._gas_client.gas_costs()
            except GasUnavailable as exc:
                log.warning("dynamic_gas_unavailable", error=repr(exc))
        return self._settings.gas_estimate, self._settings.gas_per_leg_estimate

    async def _fetch_books(self, token_ids: set[str]) -> dict[str, OrderBook]:
        """Fetch books concurrently; skip tokens without a live book (404) or transient errors."""

        async def one(token_id: str) -> tuple[str, OrderBook | None]:
            try:
                return token_id, await self._clob.get_order_book(token_id)
            except httpx.HTTPError:
                return token_id, None
            except Exception as exc:
                # A malformed CLOB payload (bad JSON / failed model validation) is not an
                # httpx error; without this it would propagate out of asyncio.gather and kill
                # the entire scan pass over one bad book. Skip the token, keep the pass alive.
                log.warning("book_fetch_failed", token_id=token_id, error=repr(exc))
                return token_id, None

        results = await asyncio.gather(*(one(t) for t in token_ids))
        return {t: b for t, b in results if b is not None}

    async def _discover(self) -> tuple[list[Event], list[Market], dict[str, Market]]:
        """Discover active binary markets (capped) and index them by condition_id."""
        s = self._settings
        events = await self._gamma.get_events(
            closed=False, active=True, limit=s.event_discovery_limit
        )
        markets: list[Market] = []
        seen: set[str] = set()
        for event in events:
            for market in event.markets:
                if (
                    market.is_binary
                    and market.active
                    and not market.closed
                    and market.accepting_orders
                    and market.condition_id not in seen
                ):
                    markets.append(market)
                    seen.add(market.condition_id)
        markets = markets[: s.max_markets_per_scan]
        by_condition: dict[str, Market] = {m.condition_id: m for m in markets}
        return events, markets, by_condition

    @staticmethod
    def _needed_tokens(events: list[Event], markets: list[Market]) -> set[str]:
        """Every token whose book a detector might read this pass: the capped global markets'
        tokens plus every multi-outcome event's live binary-constituent tokens (both sides)."""
        tokens: set[str] = set()
        for m in markets:
            tokens.update(m.clob_token_ids[:2])
        for event in events:
            if not event.is_multi_outcome:
                continue
            for m in event.markets:
                if (
                    m.is_binary
                    and m.clob_token_ids
                    and m.active
                    and not m.closed
                    and m.accepting_orders
                ):
                    tokens.update(m.clob_token_ids[:2])
        return tokens

    def _detect(
        self,
        events: list[Event],
        markets: list[Market],
        by_condition: dict[str, Market],
        books: dict[str, OrderBook],
        gas_fixed: Decimal,
        gas_per_leg: Decimal,
        now: datetime,
    ) -> list[Opportunity]:
        """Run every detector against an already-acquired ``books`` dict (no fetching here).

        Shared by the REST path and the streaming path — the only difference between them is
        where ``books`` comes from (a REST fetch vs the live cache) and, for streaming, the
        REST-confirm gate applied to the results downstream.
        """
        opps: list[Opportunity] = []
        global_snap = Snapshot(
            books=books,
            markets=markets,
            relations=self._relations,
            gas=gas_fixed,
            gas_per_leg=gas_per_leg,
            days_to_resolution=_days_to_resolution(markets, now),
        )
        opps.extend(self._complement.detect(global_snap))
        opps.extend(self._dependency.detect(global_snap))
        for event in events:
            if not event.is_multi_outcome:
                continue
            for market in event.markets:
                by_condition.setdefault(market.condition_id, market)
            event_snap = Snapshot(
                books=books,
                event=event,
                gas=gas_fixed,
                gas_per_leg=gas_per_leg,
                days_to_resolution=_days_to_resolution(event.markets, now),
            )
            opps.extend(self._negrisk.detect(event_snap))
            opps.extend(self._negrisk_dual.detect(event_snap))
            if self._settings.enable_partial_baskets:  # §5 — opt-in directional, off by default
                opps.extend(self._partial.detect(event_snap))
        return opps

    async def _emit(
        self, opps: list[Opportunity], by_condition: dict[str, Market], *, stale_dropped: int
    ) -> list[Opportunity]:
        """Risk-tag, filter, rank, persist + notify, and update metrics. Returns the kept opps."""
        for opp in opps:
            opp.resolution_risk = resolution_risk_for(opp, by_condition)

        filt = OpportunityFilter(self._settings, self._dedupe)
        kept = rank(filt.apply(opps))

        emitted = 0  # count opps actually PERSISTED, not len(kept) — a store failure shouldn't
        # inflate the metric exactly when the system is degraded (disk full, SQLite locked).
        for opp in kept:
            # Guard each emit independently: a store/notify failure on one opp must not abort the
            # loop and silently drop the rest (already marked "seen" in the dedupe cache).
            try:
                self._store.record(opp)
                emitted += 1  # persisted; matches store.count(). notify is best-effort below.
                await self._notifier.notify(opp)
                log.info(
                    "opportunity",
                    detector=str(opp.detector),
                    net_bps=str(opp.net_profit_bps),
                    size=str(opp.executable_size),
                    risk=opp.resolution_risk,
                    realizes=opp.realizes,
                    desc=opp.description,
                )
            except Exception as exc:
                log.error("emit_failed", detector=str(opp.detector), error=repr(exc))
        candidates_by_detector = Counter(str(opp.detector) for opp in opps)
        self._totals["passes"] += 1
        self._totals["candidates"] += len(opps)
        self._totals["emitted"] += emitted
        metrics.SCAN_PASSES.inc()
        metrics.EMITTED.inc(emitted)
        for detector_name, n in candidates_by_detector.items():
            self._totals[f"candidates.{detector_name}"] += n
            metrics.CANDIDATES.labels(detector=detector_name).inc(n)
        log.info(
            "scan_complete",
            candidates=len(opps),
            candidates_by_detector=dict(candidates_by_detector),
            stale_dropped=stale_dropped,
            **vars(filt.stats),
        )
        return kept

    async def scan_once(self) -> list[Opportunity]:
        """One REST scan pass: discover → fetch books → detect → filter/rank → emit."""
        events, markets, by_condition = await self._discover()
        now = datetime.now(UTC)  # one reference point for the whole pass (see _fresh_books)
        needed = self._needed_tokens(events, markets)
        fetched = await self._fetch_books(needed)
        books = _fresh_books(fetched, now, self._settings.max_book_age_s)
        stale_dropped = len(fetched) - len(books)
        log.info("scan_fetched", events=len(events), markets=len(markets), books=len(books))
        gas_fixed, gas_per_leg = await self._resolve_gas()
        opps = self._detect(events, markets, by_condition, books, gas_fixed, gas_per_leg, now)
        return await self._emit(opps, by_condition, stale_dropped=stale_dropped)

    async def _scan_streaming_once(self) -> list[Opportunity]:
        """One streaming scan pass: discover → read books from the live WS cache → detect
        CANDIDATES → REST-confirm each (R1) → filter/rank → emit the confirmed (fresh) opps.

        Streaming is a low-latency *trigger*: a candidate detected against the in-memory cache is
        re-validated against fresh REST books (``confirm_candidate``) before it is emitted, so a
        phantom edge from a dropped delta can never be reported. Only candidates pay a REST
        round-trip; discovery + detection run off the cache, preserving the CPU/IO win.
        """
        assert self._cache is not None
        events, markets, by_condition = await self._discover()
        now = datetime.now(UTC)
        needed = self._needed_tokens(events, markets)
        # Keep the runner subscribed/resynced to the current discovery set (R6, dynamic sub).
        # Read R2-fresh books from the runner (drops feed-silent tokens); fall back to the raw
        # cache when no runner is attached (the direct-call unit-test path).
        if self._streaming is not None:
            self._streaming.set_tokens(needed)
            cached = self._streaming.fresh_books(self._settings.ws_freshness_s)
        else:
            cached = self._cache.books()
        # Scope to the tokens this pass cares about.
        books = {t: b for t, b in cached.items() if t in needed}
        gas_fixed, gas_per_leg = await self._resolve_gas()
        candidates = self._detect(events, markets, by_condition, books, gas_fixed, gas_per_leg, now)

        ctx = ConfirmContext(
            markets_by_condition=by_condition,
            events_by_id={e.id: e for e in events},
            relations=self._relations,
            gas_fixed=gas_fixed,
            gas_per_leg=gas_per_leg,
            days_to_resolution=_days_to_resolution(list(by_condition.values()), now),
        )
        confirmed: list[Opportunity] = []
        for candidate in candidates:
            fresh = await confirm_candidate(
                candidate, ctx=ctx, clob=self._clob, detectors=self._detectors_by_kind
            )
            if fresh is not None:
                confirmed.append(fresh)
        log.info(
            "scan_streamed",
            cached_books=len(books),
            candidates=len(candidates),
            confirmed=len(confirmed),
        )
        return await self._emit(confirmed, by_condition, stale_dropped=0)

    async def run(self, *, passes: int = 0, max_seconds: float | None = None) -> None:
        """Loop ``scan_once`` on the configured interval until done or signalled.

        ``passes=0`` loops indefinitely. Stops cleanly on SIGINT/SIGTERM (the inter-pass
        sleep is interruptible) so a containerised scanner shuts down gracefully.
        """
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # not all platforms support this
                loop.add_signal_handler(sig, stop.set)

        # Start the streaming runner (phase 3) when enabled: it keeps self._cache fresh from the
        # market WS, sharing this scanner's ClobClient/limiter for resync (R7). Scan passes then
        # read books from the cache and REST-confirm each candidate before emit.
        streaming_task: asyncio.Task[None] | None = None
        if self._streaming_enabled and self._cache is not None:
            # Best-effort initial subscription set: a discovery hiccup at startup must not crash the
            # scanner. On failure the runner starts with no tokens; each scan pass repopulates it
            # via set_tokens, and the periodic resync fills the cache.
            try:
                init_events, init_markets, _ = await self._discover()
                init_tokens = sorted(self._needed_tokens(init_events, init_markets))
            except Exception as exc:
                log.warning("streaming_init_discover_failed", error=repr(exc))
                init_tokens = []
            self._streaming = StreamingBooks(
                token_ids=init_tokens,
                clob=self._clob,
                settings=self._settings,
                cache=self._cache,
                ws_factory=self._ws_factory,
            )
            streaming_task = asyncio.create_task(self._streaming.run(stop))
            log.info("streaming_started", tokens=len(init_tokens))
        scan_pass = self._scan_streaming_once if self._streaming is not None else self.scan_once

        start = loop.time()
        attempts = 0  # scan-pass invocations (success or failure); `passes` arg bounds this
        try:
            while not stop.is_set():
                attempts += 1
                try:
                    await scan_pass()
                except Exception as exc:  # a bad pass must not kill the loop
                    self._totals["errors"] += 1
                    metrics.SCAN_ERRORS.inc()
                    log.error("scan_pass_failed", error=repr(exc))
                # D7-heartbeat: pulse after every attempt (success OR error).  A wedged
                # loop stops pulsing; a crashing-then-sleeping loop keeps pulsing (alive).
                # Both the Prometheus gauge and the heartbeat file are updated here.
                _ts = _now()
                metrics.LAST_PASS.set(_ts)
                _write_heartbeat(self._settings.heartbeat_path)
                if passes and attempts >= passes:
                    break
                if max_seconds is not None and (loop.time() - start) >= max_seconds:
                    break
                # Interruptible sleep: wakes early if a stop signal arrives.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=self._settings.scan_interval_seconds
                    )
        finally:
            # Stop the streaming runner (it watches `stop`, but passes/max_seconds exits leave it
            # unset) and await its clean shutdown before releasing shared clients.
            stop.set()
            if streaming_task is not None:
                with contextlib.suppress(Exception):
                    await streaming_task
            # Release the notifier's owned HTTP client (a WebhookNotifier creates one) on every
            # exit path — signal, passes/max_seconds, or error — so we don't leak the pool.
            with contextlib.suppress(Exception):
                await self._notifier.aclose()
            if self._gas_client is not None:
                with contextlib.suppress(Exception):
                    await self._gas_client.aclose()
            # `attempts` counts scan_once invocations; self._totals["passes"] counts the ones
            # that completed without error, so a failed pass no longer logs a contradictory pair.
            log.info("scanner_stopped", attempts=attempts, totals=dict(self._totals))
