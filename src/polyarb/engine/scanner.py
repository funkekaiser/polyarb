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
from datetime import UTC, datetime

import httpx
import structlog

from polyarb.clients.clob import ClobClient
from polyarb.clients.gamma import GammaClient
from polyarb.config import Settings
from polyarb.detectors.base import Snapshot
from polyarb.detectors.complement import ComplementDetector
from polyarb.detectors.dependency import DependencyDetector
from polyarb.detectors.negrisk_basket import NegRiskBasketDetector, NegRiskDualDetector
from polyarb.detectors.partial_basket import PartialBasketDetector
from polyarb.engine import metrics
from polyarb.engine.filters import DedupeCache, OpportunityFilter
from polyarb.engine.ranking import rank
from polyarb.models import DetectorKind, Market, Opportunity, OrderBook
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
    ) -> None:
        self._settings = settings
        self._gamma = gamma
        self._clob = clob
        self._store = store
        self._notifier = notifier or NullNotifier()
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
        self._dedupe = DedupeCache(settings.dedupe_cooldown_seconds)
        # Cumulative metrics across passes (Prometheus /metrics could expose these later).
        self._totals: Counter[str] = Counter()

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

    async def scan_once(self) -> list[Opportunity]:
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

        token_ids: set[str] = set()
        for market in markets:
            token_ids.update(market.clob_token_ids[:2])
        now = datetime.now(UTC)  # one reference point for the whole pass (see _fresh_books)
        fetched = await self._fetch_books(token_ids)
        books = _fresh_books(fetched, now, s.max_book_age_s)
        stale_dropped = len(fetched) - len(books)  # accumulates per-event extra drops below
        log.info(
            "scan_fetched",
            events=len(events),
            markets=len(markets),
            books=len(books),
            global_stale_dropped=stale_dropped,  # global only; the pass total is in scan_complete
        )

        opps: list[Opportunity] = []
        global_snap = Snapshot(
            books=books,
            markets=markets,
            relations=self._relations,
            gas=s.gas_estimate,
            gas_per_leg=s.gas_per_leg_estimate,
            days_to_resolution=_days_to_resolution(markets, now),
        )
        opps.extend(self._complement.detect(global_snap))
        opps.extend(self._dependency.detect(global_snap))

        for event in events:
            if not event.is_multi_outcome:
                continue
            # Fetch books for *live* constituents only (eliminated outcomes are dropped from the
            # partition). Both tokens: the YES basket uses YES books, the NO-dual uses NO books.
            needed = {
                tid
                for m in event.markets
                if m.is_binary
                and m.clob_token_ids
                and m.active
                and not m.closed
                and m.accepting_orders
                for tid in m.clob_token_ids[:2]
            } - fetched.keys()  # fetched (not books): a stale global token is already dropped —
            # don't waste a round-trip re-fetching it just to drop it again.
            extra = await self._fetch_books(needed) if needed else {}
            fresh_extra = _fresh_books(extra, now, s.max_book_age_s)
            stale_dropped += len(extra) - len(fresh_extra)
            event_books = books | fresh_extra
            for market in event.markets:
                by_condition.setdefault(market.condition_id, market)
            event_snap = Snapshot(
                books=event_books,
                event=event,
                gas=s.gas_estimate,
                gas_per_leg=s.gas_per_leg_estimate,
                days_to_resolution=_days_to_resolution(event.markets, now),
            )
            opps.extend(self._negrisk.detect(event_snap))
            opps.extend(self._negrisk_dual.detect(event_snap))
            if s.enable_partial_baskets:  # §5 — opt-in directional, off by default
                opps.extend(self._partial.detect(event_snap))

        for opp in opps:
            opp.resolution_risk = resolution_risk_for(opp, by_condition)

        filt = OpportunityFilter(s, self._dedupe)
        kept = rank(filt.apply(opps))

        emitted = 0  # count opps actually PERSISTED, not len(kept) — a store failure shouldn't
        # inflate the metric exactly when the system is degraded (disk full, SQLite locked) and
        # accurate monitoring matters most. (Counts after record; notify is best-effort.)
        for opp in kept:
            # Guard each emit independently: a store/notify failure on one opp must not abort
            # the loop and silently drop the rest (they were already marked "seen" in the
            # dedupe cache during filtering, so an aborted loop would suppress them for a full
            # cooldown window).
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
        # filt.stats spreads seen / below_profit / below_notional / at_risk / deduped / kept
        # ("kept" = passed filters; the persisted count is `emitted` in scanner_stopped totals).
        log.info(
            "scan_complete",
            candidates=len(opps),
            candidates_by_detector=dict(candidates_by_detector),
            stale_dropped=stale_dropped,
            **vars(filt.stats),
        )
        return kept

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

        start = loop.time()
        attempts = 0  # scan_once invocations (success or failure); `passes` arg bounds this
        try:
            while not stop.is_set():
                attempts += 1
                try:
                    await self.scan_once()
                except Exception as exc:  # a bad pass must not kill the loop
                    self._totals["errors"] += 1
                    metrics.SCAN_ERRORS.inc()
                    log.error("scan_pass_failed", error=repr(exc))
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
            # Release the notifier's owned HTTP client (a WebhookNotifier creates one) on every
            # exit path — signal, passes/max_seconds, or error — so we don't leak the pool.
            with contextlib.suppress(Exception):
                await self._notifier.aclose()
            # `attempts` counts scan_once invocations; self._totals["passes"] counts the ones
            # that completed without error, so a failed pass no longer logs a contradictory pair.
            log.info("scanner_stopped", attempts=attempts, totals=dict(self._totals))
