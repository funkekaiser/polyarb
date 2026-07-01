"""Realized-outcome settlement math for the E1 ledger — pure, offline, deterministic.

Given a locked :class:`~polyarb.models.Opportunity` and the *resolved* prices of its legs'
tokens, compute the realized payoff and P&L. This is where "guaranteed" is finally audited
against reality: a structural lock that settles negative (via a 50-50 void, a mis-declared
relation, or an unfilled leg) is caught here, and the per-leg void signal is exactly the
A2-void measurement the committee deferred to E1.

No network, no clients — the caller (the read-only ``settle`` poller, E1-c) fetches the
resolved markets and hands the prices in.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from polyarb.models import Market, Opportunity
from polyarb.sinks.store import OpportunityStore

_ZERO = Decimal(0)
_ONE = Decimal(1)


class MarketResolver(Protocol):
    """Read-only source of resolved markets by condition id (satisfied by ``GammaClient``)."""

    async def resolved_markets(self, condition_ids: list[str]) -> list[Market]: ...


@dataclass(frozen=True)
class SettlementRun:
    """Summary of one poller pass over the pending ledger."""

    checked: int
    settled: int  # resolved cleanly (all legs 0/1)
    void: int  # settled but a leg voided off {0,1}
    still_pending: int  # at least one leg not yet resolved


@dataclass(frozen=True)
class SettlementResult:
    """The audited outcome of one locked opportunity."""

    status: str  # "resolved" (clean 0/1 legs) | "void" (a leg settled off {0,1}, e.g. 50-50)
    realized_payoff: Decimal  # long-side settlement receipts (Σ resolved_price · size over buys)
    realized_pnl: Decimal  # net profit vs entry, gas-adjusted (authoritative figure)
    detail: dict[str, object]  # per-leg resolved price, for the audit trail


def token_resolution_map(markets: list[Market]) -> dict[str, Decimal]:
    """Map ``token_id -> resolved price`` from each **closed** market's outcome prices.

    Open markets are skipped (their ``outcome_prices`` are live, not settlements). A resolved
    binary settles at 0 or 1; a 50-50 void settles both tokens at 0.5.
    """
    out: dict[str, Decimal] = {}
    for market in markets:
        if not market.closed:
            continue
        if len(market.clob_token_ids) != len(market.outcome_prices):
            continue  # malformed / not-yet-populated resolution
        for token_id, price in zip(market.clob_token_ids, market.outcome_prices, strict=False):
            out[token_id] = price
    return out


def settle(opp: Opportunity, resolved: dict[str, Decimal]) -> SettlementResult | None:
    """Realized P&L for a locked ``opp`` given resolved token prices.

    Returns ``None`` when any leg's token is not yet resolved (the event stays pending — we
    never settle a partially-resolved basket). A leg settling off ``{0, 1}`` (a 50-50 void or
    any anomalous partial) flags the whole result ``void``.

    Per leg, relative to the entry price paid/received:
      * buy  → ``size · (resolved - entry)``   (paid entry, receives resolved at settlement)
      * sell → ``size · (entry - resolved)``   (received entry, owes resolved at settlement)
    """
    if not opp.legs:
        return None
    for leg in opp.legs:
        if leg.token_id not in resolved:
            return None  # a leg's market hasn't resolved yet → still pending

    payoff = _ZERO
    pnl = _ZERO
    voided = False
    detail: dict[str, object] = {}
    for leg in opp.legs:
        resolved_price = resolved[leg.token_id]
        detail[leg.token_id] = str(resolved_price)
        if resolved_price not in (_ZERO, _ONE):
            voided = True  # 50-50 void or anomalous partial settlement
        if leg.side == "buy":
            payoff += resolved_price * leg.size
            pnl += leg.size * (resolved_price - leg.price)
        else:  # sell / short
            pnl += leg.size * (leg.price - resolved_price)
    pnl -= opp.gas

    return SettlementResult(
        status="void" if voided else "resolved",
        realized_payoff=payoff,
        realized_pnl=pnl,
        detail=detail,
    )


async def poll_settlements(
    store: OpportunityStore,
    resolver: MarketResolver,
    *,
    batch_limit: int = 500,
) -> SettlementRun:
    """Read-only pass: settle any pending ledger event whose legs have all resolved (E1-c).

    Loads pending economic events, fetches the resolutions of their condition ids in one batch
    (Gamma reads only — never touches a signing client), settles each fully-resolved event, and
    writes the realized outcome back. Events with an unresolved leg are left pending for a later
    pass. Safe to call on a slow cadence from the scanner or manually via ``polyarb settle``.
    """
    entries = store.pending_events(limit=batch_limit)
    if not entries:
        return SettlementRun(checked=0, settled=0, void=0, still_pending=0)

    condition_ids = sorted({cid for entry in entries for cid in entry.opp.condition_ids})
    markets = await resolver.resolved_markets(condition_ids)
    resolved = token_resolution_map(markets)

    settled = void = still_pending = 0
    for entry in entries:
        result = settle(entry.opp, resolved)
        if result is None:
            still_pending += 1
            continue
        store.record_resolution(
            entry.fingerprint,
            status=result.status,
            realized_payoff=result.realized_payoff,
            realized_pnl=result.realized_pnl,
            detail=result.detail,
        )
        if result.status == "void":
            void += 1
        else:
            settled += 1

    return SettlementRun(
        checked=len(entries), settled=settled, void=void, still_pending=still_pending
    )
