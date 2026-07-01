"""Offline analytics for stored arbitrage opportunities (the ``backtest`` feature).

Aggregates a list of :class:`~polyarb.models.Opportunity` objects into a
:class:`BacktestSummary` and renders it as a human-readable report.  All
arithmetic uses :class:`~decimal.Decimal` to avoid float drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polyarb.models import DetectorKind, Opportunity
from polyarb.sinks.store import LedgerEntry

_ZERO = Decimal(0)
_TWO = Decimal(2)


@dataclass(frozen=True)
class BacktestSummary:
    """Aggregate statistics for a collection of opportunities."""

    total: int
    by_detector: dict[str, int]
    by_risk: dict[str, int]
    by_realizes: dict[str, int]
    net_bps_min: Decimal
    net_bps_median: Decimal
    net_bps_max: Decimal
    net_bps_mean: Decimal
    total_would_be_pnl: Decimal  # Σ total_net_profit over STRUCTURAL opps (gas-adjusted)
    # Σ total_net_profit over PARTIAL_BASKET opps — a directional *expected* value (optimistic,
    # NOT a structural guarantee); kept separate so it never inflates the structural headline.
    directional_ev: Decimal
    total_executable_notional: Decimal  # Σ cost * executable_size
    avg_days_to_resolution: float | None  # mean over opps with days_to_resolution; None if none


def _decimal_median(values: list[Decimal]) -> Decimal:
    """Return the median of a *sorted* non-empty list using Decimal arithmetic.

    Even-length lists: average of the two middle elements.
    """
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / _TWO


def summarize(opps: list[Opportunity]) -> BacktestSummary:
    """Compute aggregate statistics for *opps*.

    Returns a well-defined zero summary for an empty input (no division by zero).
    """
    if not opps:
        return BacktestSummary(
            total=0,
            by_detector={},
            by_risk={},
            by_realizes={},
            net_bps_min=_ZERO,
            net_bps_median=_ZERO,
            net_bps_max=_ZERO,
            net_bps_mean=_ZERO,
            total_would_be_pnl=_ZERO,
            directional_ev=_ZERO,
            total_executable_notional=_ZERO,
            avg_days_to_resolution=None,
        )

    by_detector: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    by_realizes: dict[str, int] = {}
    bps_values: list[Decimal] = []
    total_pnl = _ZERO  # structural only
    directional_ev = _ZERO  # PARTIAL_BASKET (directional, optimistic EV) — kept separate
    total_notional = _ZERO
    days_sum = 0
    days_count = 0

    for opp in opps:
        # Counts by category
        key_det = str(opp.detector)
        by_detector[key_det] = by_detector.get(key_det, 0) + 1

        key_risk = opp.resolution_risk if opp.resolution_risk is not None else "unknown"
        by_risk[key_risk] = by_risk.get(key_risk, 0) + 1

        by_realizes[opp.realizes] = by_realizes.get(opp.realizes, 0) + 1

        # Bps distribution
        bps_values.append(opp.net_profit_bps)

        # P&L and notional — structural P&L and directional EV are NOT comparable, so split them.
        if opp.detector == DetectorKind.PARTIAL_BASKET:
            directional_ev += opp.total_net_profit
        else:
            total_pnl += opp.total_net_profit
        total_notional += opp.cost * opp.executable_size

        # Days-to-resolution (only opps that have it)
        if opp.days_to_resolution is not None:
            days_sum += opp.days_to_resolution
            days_count += 1

    bps_sorted = sorted(bps_values)
    n = len(bps_sorted)
    bps_sum = sum(bps_sorted, _ZERO)

    return BacktestSummary(
        total=n,
        by_detector=by_detector,
        by_risk=by_risk,
        by_realizes=by_realizes,
        net_bps_min=bps_sorted[0],
        net_bps_median=_decimal_median(bps_sorted),
        net_bps_max=bps_sorted[-1],
        net_bps_mean=bps_sum / Decimal(n),
        total_would_be_pnl=total_pnl,
        directional_ev=directional_ev,
        total_executable_notional=total_notional,
        avg_days_to_resolution=days_sum / days_count if days_count > 0 else None,
    )


def format_summary(summary: BacktestSummary) -> str:
    """Render *summary* as a multi-line human-readable report."""
    lines: list[str] = []
    q1 = Decimal("0.1")  # bps display precision
    q2 = Decimal("0.01")  # money display precision

    lines.append(
        f"Backtest summary — {summary.total} opportunit{'y' if summary.total == 1 else 'ies'}"
    )
    lines.append("")

    # Category breakdowns
    def _breakdown(label: str, counts: dict[str, int]) -> None:
        lines.append(f"  {label}:")
        if not counts:
            lines.append("    (none)")
            return
        for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {key}: {n}")

    _breakdown("by detector", summary.by_detector)
    _breakdown("by resolution risk", summary.by_risk)
    _breakdown("by realizes", summary.by_realizes)
    lines.append("")

    # Bps stats
    lines.append("  net profit (bps):")
    lines.append(f"    min:    {summary.net_bps_min.quantize(q1)}")
    lines.append(f"    median: {summary.net_bps_median.quantize(q1)}")
    lines.append(f"    mean:   {summary.net_bps_mean.quantize(q1)}")
    lines.append(f"    max:    {summary.net_bps_max.quantize(q1)}")
    lines.append("")

    # P&L and notional
    lines.append(f"  structural would-be P&L:     ${summary.total_would_be_pnl.quantize(q2)}")
    if summary.directional_ev != _ZERO:
        lines.append(
            f"  directional EV (partial):    ${summary.directional_ev.quantize(q2)}  "
            "(optimistic, NOT a structural guarantee)"
        )
    lines.append(
        f"  total executable notional:   ${summary.total_executable_notional.quantize(q2)}"
    )
    lines.append("")

    # Days to resolution
    if summary.avg_days_to_resolution is not None:
        lines.append(f"  avg days to resolution: {summary.avg_days_to_resolution:.1f}")
    else:
        lines.append("  avg days to resolution: n/a")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# E1-d — realized-outcome summary over the ledger (C4 truth, not the would-be
# upper bound). Reads settled `economic_events`; a settled event with
# realized_pnl < 0 is a "guaranteed" arb that actually went bad (E2 alerts on it).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerSummary:
    """Realized outcomes across distinct economic events (deduped)."""

    total_events: int
    pending: int
    settled: int  # resolved + void — events with a realized P&L
    void: int  # settled where a leg voided off {0,1}
    realized_pnl: Decimal  # Σ realized_pnl over settled events (the real number)
    wins: int  # settled with realized_pnl > 0
    losses: int  # settled with realized_pnl < 0
    worst_loss: Decimal  # most-negative realized_pnl (0 if no losses)


def summarize_ledger(entries: list[LedgerEntry]) -> LedgerSummary:
    """Aggregate realized outcomes. A well-defined zero summary for an empty ledger."""
    pending = settled = void = wins = losses = 0
    realized_pnl = _ZERO
    worst_loss = _ZERO
    for entry in entries:
        if entry.status == "pending" or entry.realized_pnl is None:
            pending += 1
            continue
        settled += 1
        if entry.status == "void":
            void += 1
        pnl = entry.realized_pnl
        realized_pnl += pnl
        if pnl > _ZERO:
            wins += 1
        elif pnl < _ZERO:
            losses += 1
            worst_loss = min(worst_loss, pnl)
    return LedgerSummary(
        total_events=len(entries),
        pending=pending,
        settled=settled,
        void=void,
        realized_pnl=realized_pnl,
        wins=wins,
        losses=losses,
        worst_loss=worst_loss,
    )


def format_ledger_summary(summary: LedgerSummary) -> str:
    """Render the realized-outcome summary. Empty ledger → a single clear line."""
    if summary.total_events == 0:
        return "Realized ledger — no economic events tracked yet."

    q2 = Decimal("0.01")
    lines = [
        f"Realized ledger — {summary.total_events} distinct economic "
        f"event{'' if summary.total_events == 1 else 's'}",
        "",
        f"  pending (unresolved): {summary.pending}",
        f"  settled:              {summary.settled}  "
        f"(wins {summary.wins}, losses {summary.losses}, void {summary.void})",
    ]
    if summary.settled:
        win_rate = Decimal(summary.wins) / Decimal(summary.settled) * Decimal(100)
        lines.append(f"  realized P&L:         ${summary.realized_pnl.quantize(q2)}")
        lines.append(f"  win rate:             {win_rate.quantize(Decimal('0.1'))}%")
        if summary.losses:
            lines.append(
                f"  worst realized loss:  ${summary.worst_loss.quantize(q2)}  "
                "(a 'guaranteed' arb that settled negative — see E2)"
            )
    return "\n".join(lines)
