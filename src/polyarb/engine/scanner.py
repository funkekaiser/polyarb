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

import httpx
import structlog

from polyarb.clients.clob import ClobClient
from polyarb.clients.gamma import GammaClient
from polyarb.config import Settings
from polyarb.detectors.base import Snapshot
from polyarb.detectors.complement import ComplementDetector
from polyarb.detectors.dependency import DependencyDetector
from polyarb.detectors.negrisk_basket import NegRiskBasketDetector
from polyarb.engine.filters import DedupeCache, OpportunityFilter
from polyarb.engine.ranking import rank
from polyarb.models import Market, Opportunity, OrderBook
from polyarb.resolution.relations import (
    POLITICS_NESTING,
    SEED_RELATIONS,
    SPORTS_NESTING,
    MarketTags,
    Relation,
    generate_dag_relations,
    generate_ladder_relations,
)
from polyarb.resolution.risk import aggregate_risk
from polyarb.sinks.notify import Notifier, NullNotifier
from polyarb.sinks.store import OpportunityStore

log = structlog.get_logger("polyarb.scanner")


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
        market_tags = tags or []
        self._relations = (
            declared
            + generate_ladder_relations(market_tags)
            + generate_dag_relations(market_tags, [*SPORTS_NESTING, *POLITICS_NESTING])
        )
        self._complement = ComplementDetector()
        self._negrisk = NegRiskBasketDetector()
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
                    and market.condition_id not in seen
                ):
                    markets.append(market)
                    seen.add(market.condition_id)
        markets = markets[: s.max_markets_per_scan]
        by_condition: dict[str, Market] = {m.condition_id: m for m in markets}

        token_ids: set[str] = set()
        for market in markets:
            token_ids.update(market.clob_token_ids[:2])
        books = await self._fetch_books(token_ids)
        log.info("scan_fetched", events=len(events), markets=len(markets), books=len(books))

        opps: list[Opportunity] = []
        global_snap = Snapshot(
            books=books, markets=markets, relations=self._relations, gas=s.gas_estimate
        )
        opps.extend(self._complement.detect(global_snap))
        opps.extend(self._dependency.detect(global_snap))

        for event in events:
            if not event.is_multi_outcome:
                continue
            needed = {
                m.yes_token_id for m in event.markets if m.is_binary and m.clob_token_ids
            } - books.keys()
            event_books = books | (await self._fetch_books(needed) if needed else {})
            for market in event.markets:
                by_condition.setdefault(market.condition_id, market)
            opps.extend(
                self._negrisk.detect(Snapshot(books=event_books, event=event, gas=s.gas_estimate))
            )

        for opp in opps:
            opp.resolution_risk = aggregate_risk(
                [by_condition[c] for c in opp.condition_ids if c in by_condition]
            )

        filt = OpportunityFilter(s, self._dedupe)
        kept = rank(filt.apply(opps))

        for opp in kept:
            self._store.record(opp)
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
        by_detector = Counter(str(opp.detector) for opp in opps)
        self._totals["passes"] += 1
        self._totals["candidates"] += len(opps)
        self._totals["emitted"] += len(kept)
        for detector_name, n in by_detector.items():
            self._totals[f"candidates.{detector_name}"] += n
        # filt.stats spreads seen / below_profit / below_notional / at_risk / deduped / emitted.
        log.info(
            "scan_complete",
            candidates=len(opps),
            by_detector=dict(by_detector),
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
        completed = 0
        while not stop.is_set():
            completed += 1
            try:
                await self.scan_once()
            except Exception as exc:  # a bad pass must not kill the loop
                self._totals["errors"] += 1
                log.error("scan_pass_failed", error=repr(exc))
            if passes and completed >= passes:
                break
            if max_seconds is not None and (loop.time() - start) >= max_seconds:
                break
            # Interruptible sleep: wakes early if a stop signal arrives.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._settings.scan_interval_seconds)

        log.info("scanner_stopped", passes=completed, totals=dict(self._totals))
