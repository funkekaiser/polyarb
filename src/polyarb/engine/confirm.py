"""REST-confirm barrier — websocket phase 3, requirement R1.

Streaming detection is a low-latency **trigger**, never the emit source (committee verdict; see
docs/STRATEGY_BACKLOG.md R1). A candidate detected against the in-memory WS cache is re-validated
against **fresh REST books** before it is emitted: a single dropped delta can fabricate a phantom
edge across the ``YES + NO = 1`` knife-edge, and the top-of-book integrity check cannot see deep
or size-only divergence. Re-fetching the candidate's *exact legs* and re-running its detector
collapses the detect→reality gap to one REST round-trip and restores a coherent cross-leg snapshot
at emit time. Only candidates pay the round-trip, so the discovery-side CPU/IO win is preserved.

The returned Opportunity is the **fresh** one (sizes/prices recomputed from authoritative REST
books), not the stale cache candidate — so the emitted size already reflects real depth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
import structlog

from polyarb.clients.clob import ClobClient
from polyarb.detectors.base import Detector, Snapshot
from polyarb.models import DetectorKind, Event, Market, Opportunity, OrderBook
from polyarb.resolution.relations import Relation

log = structlog.get_logger("polyarb.confirm")

# Detectors whose snapshot is scoped to an Event (vs the global markets list).
_EVENT_DETECTORS: frozenset[DetectorKind] = frozenset(
    {DetectorKind.NEGRISK_BASKET, DetectorKind.NEGRISK_DUAL, DetectorKind.PARTIAL_BASKET}
)


@dataclass
class ConfirmContext:
    """Discovery-side context needed to rebuild a candidate's snapshot for confirmation.

    Populated by the scanner from the same discovery pass that fed the streaming cache — the
    confirm step only re-fetches *books*, reusing the already-known market/event metadata.
    """

    markets_by_condition: dict[str, Market] = field(default_factory=dict)
    events_by_id: dict[str, Event] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    gas_fixed: Decimal = Decimal(0)
    gas_per_leg: Decimal = Decimal(0)
    days_to_resolution: dict[str, int] = field(default_factory=dict)


def _leg_signature(opp: Opportunity) -> frozenset[tuple[str, str]]:
    """The ``(token_id, side)`` set that uniquely identifies the trade STRUCTURE — complement
    under (buy/buy) vs over (sell/sell), YES-basket vs NO-dual — independent of the recomputed
    prices/sizes. Confirmation requires the SAME structure, not merely any opp on the market."""
    return frozenset((leg.token_id, leg.side) for leg in opp.legs)


async def _fetch_books(clob: ClobClient, token_ids: set[str]) -> dict[str, OrderBook]:
    """Fetch fresh REST books for ``token_ids`` concurrently; skip a token on any fetch error.

    A missing/failed book makes the re-run detector short-circuit → the candidate is simply not
    confirmed (a false negative, never a false positive). Mirrors ``Scanner._fetch_books``.
    """

    async def one(token_id: str) -> tuple[str, OrderBook | None]:
        try:
            return token_id, await clob.get_order_book(token_id)
        except httpx.HTTPError:
            return token_id, None
        except Exception as exc:  # malformed payload etc. — never let it abort a confirm
            log.warning("confirm_book_fetch_failed", token_id=token_id, error=repr(exc))
            return token_id, None

    results = await asyncio.gather(*(one(t) for t in token_ids))
    return {t: b for t, b in results if b is not None}


def _build_snapshot(
    candidate: Opportunity, ctx: ConfirmContext, books: dict[str, OrderBook]
) -> Snapshot | None:
    """Rebuild the detector's input snapshot scoped to the candidate, with fresh ``books``."""
    if candidate.detector in _EVENT_DETECTORS:
        event = ctx.events_by_id.get(candidate.event_id) if candidate.event_id else None
        if event is None:
            return None
        return Snapshot(
            books=books,
            event=event,
            gas=ctx.gas_fixed,
            gas_per_leg=ctx.gas_per_leg,
            days_to_resolution=ctx.days_to_resolution,
        )
    markets = [
        ctx.markets_by_condition[c]
        for c in candidate.condition_ids
        if c in ctx.markets_by_condition
    ]
    if not markets:
        return None
    return Snapshot(
        books=books,
        markets=markets,
        relations=ctx.relations,
        gas=ctx.gas_fixed,
        gas_per_leg=ctx.gas_per_leg,
        days_to_resolution=ctx.days_to_resolution,
    )


async def confirm_candidate(
    candidate: Opportunity,
    *,
    ctx: ConfirmContext,
    clob: ClobClient,
    detectors: Mapping[DetectorKind, Detector],
) -> Opportunity | None:
    """Re-validate a streamed candidate against fresh REST books.

    Returns the FRESH, authoritative Opportunity (recomputed from re-fetched books) whose trade
    structure matches the candidate's leg signature, or ``None`` if the edge no longer holds (or
    the candidate's detector/market/event context is unavailable, or all leg books fail to fetch).
    The caller emits the returned opp, never the stale candidate.
    """
    detector = detectors.get(candidate.detector)
    if detector is None:
        return None
    token_ids = {leg.token_id for leg in candidate.legs}
    if not token_ids:
        return None
    books = await _fetch_books(clob, token_ids)
    snap = _build_snapshot(candidate, ctx, books)
    if snap is None:
        return None
    target = _leg_signature(candidate)
    for fresh in detector.detect(snap):
        if _leg_signature(fresh) == target:
            return fresh  # same trade structure still holds on fresh books → emit this
    return None
