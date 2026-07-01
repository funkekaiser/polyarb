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

from polyarb.models import DetectorKind, Market, Opportunity
from polyarb.sinks.notify import Notifier
from polyarb.sinks.store import OpportunityStore

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HALF = Decimal("0.5")
# Void band. Gamma outcome_prices at resolution converge to ~1 / ~0 but are NOT exact
# (live-verified 2026-07-01: winners ~0.9999, losers ~1e-6; a 50-50 void sits near 0.5), so we
# round to the nearest on-chain payout {0, 0.5, 1}. Prices in the middle band have no clear
# winner → treated as a void (see docs/API_NOTES.md).
_VOID_LO = Decimal("0.25")
_VOID_HI = Decimal("0.75")


def _settled_payout(price: Decimal) -> tuple[Decimal, bool]:
    """Map a resolved outcome price to its payout {0, 0.5, 1} and a void flag."""
    if price >= _VOID_HI:
        return _ONE, False
    if price <= _VOID_LO:
        return _ZERO, False
    return _HALF, True


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
    alerted: int = 0  # E2 — structural locks that settled negative (an audited failure)


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
        payout, is_void = _settled_payout(resolved_price)
        # Record the raw price for the audit trail, but settle on the rounded on-chain payout.
        detail[leg.token_id] = str(resolved_price)
        if is_void:
            voided = True  # near-0.5 → no clear winner → 50-50 void
        if leg.side == "buy":
            payoff += payout * leg.size
            pnl += leg.size * (payout - leg.price)
        else:  # sell / short
            pnl += leg.size * (leg.price - payout)
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
    notifier: Notifier | None = None,
    batch_limit: int = 500,
) -> SettlementRun:
    """Read-only pass: settle any pending ledger event whose legs have all resolved (E1-c).

    Loads pending economic events, fetches the resolutions of their condition ids in one batch
    (Gamma reads only — never touches a signing client), settles each fully-resolved event, and
    writes the realized outcome back. Events with an unresolved leg are left pending for a later
    pass. Safe to call on a slow cadence from the scanner or manually via ``polyarb settle``.

    E2 — when a **structural** ("guaranteed") lock settles with realized P&L < 0, fire an audit
    alert via ``notifier``. This catches the failure modes a model-free lock can still hit — a
    50-50 void (A2-void), a mis-declared relation (D1/D2), an unfilled leg. Directional partial
    baskets are excluded (they're EV bets, expected to sometimes lose). Each event settles exactly
    once (it leaves the pending set), so it alerts at most once — no extra dedupe needed.
    """
    entries = store.pending_events(limit=batch_limit)
    if not entries:
        return SettlementRun(checked=0, settled=0, void=0, still_pending=0, alerted=0)

    condition_ids = sorted({cid for entry in entries for cid in entry.opp.condition_ids})
    markets = await resolver.resolved_markets(condition_ids)
    resolved = token_resolution_map(markets)

    settled = void = still_pending = alerted = 0
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

        # E2 — a structural lock that settled negative is an audit failure worth an alarm.
        if (
            notifier is not None
            and result.realized_pnl < _ZERO
            and entry.opp.detector != DetectorKind.PARTIAL_BASKET
        ):
            alerted += 1
            await notifier.alert(
                "polyarb: guaranteed arb settled NEGATIVE",
                f"{entry.opp.detector} {entry.fingerprint} realized "
                f"${result.realized_pnl} (status={result.status}) :: {entry.opp.description}",
            )

    return SettlementRun(
        checked=len(entries),
        settled=settled,
        void=void,
        still_pending=still_pending,
        alerted=alerted,
    )
