"""Offline tests for the backtest analytics module (no live API calls)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from polyarb.engine.backtest import (
    format_ledger_summary,
    format_shadow_summary,
    format_summary,
    summarize,
    summarize_ledger,
    summarize_shadow_arrivals,
)
from polyarb.models import DetectorKind, Opportunity
from polyarb.sinks.store import LedgerEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opp(
    *,
    detector: DetectorKind = DetectorKind.COMPLEMENT,
    net_profit_bps: str,
    net_profit: str = "0.10",
    executable_size: str = "100",
    cost: str = "0.90",
    realizes: str = "instant",
    resolution_risk: str | None = None,
    days_to_resolution: int | None = None,
) -> Opportunity:
    return Opportunity(
        detector=detector,
        description="t",
        condition_ids=["0x1"],
        legs=[],
        cost=Decimal(cost),
        gross_profit=Decimal("0.10"),
        fees=Decimal(0),
        gas=Decimal(0),
        net_profit=Decimal(net_profit),
        net_profit_bps=Decimal(net_profit_bps),
        executable_size=Decimal(executable_size),
        realizes=realizes,  # type: ignore[arg-type]
        resolution_risk=resolution_risk,
        days_to_resolution=days_to_resolution,
    )


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_input_returns_zero_summary() -> None:
    s = summarize([])
    assert s.total == 0
    assert s.by_detector == {}
    assert s.by_risk == {}
    assert s.by_realizes == {}
    assert s.net_bps_min == Decimal(0)
    assert s.net_bps_median == Decimal(0)
    assert s.net_bps_max == Decimal(0)
    assert s.net_bps_mean == Decimal(0)
    assert s.total_would_be_pnl == Decimal(0)
    assert s.total_executable_notional == Decimal(0)
    assert s.avg_days_to_resolution is None


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def test_total_count() -> None:
    opps = [_opp(net_profit_bps="100"), _opp(net_profit_bps="200"), _opp(net_profit_bps="300")]
    assert summarize(opps).total == 3


def test_by_detector_counts() -> None:
    opps = [
        _opp(detector=DetectorKind.COMPLEMENT, net_profit_bps="100"),
        _opp(detector=DetectorKind.COMPLEMENT, net_profit_bps="200"),
        _opp(detector=DetectorKind.NEGRISK_BASKET, net_profit_bps="300"),
    ]
    s = summarize(opps)
    assert s.by_detector == {"complement": 2, "negrisk_basket": 1}


def test_by_risk_counts_unknown_when_none() -> None:
    opps = [
        _opp(net_profit_bps="100", resolution_risk="objective"),
        _opp(net_profit_bps="200", resolution_risk=None),
        _opp(net_profit_bps="300", resolution_risk=None),
    ]
    s = summarize(opps)
    assert s.by_risk == {"objective": 1, "unknown": 2}


def test_by_realizes_counts() -> None:
    opps = [
        _opp(net_profit_bps="100", realizes="instant"),
        _opp(net_profit_bps="200", realizes="instant"),
        _opp(net_profit_bps="300", realizes="resolution"),
    ]
    s = summarize(opps)
    assert s.by_realizes == {"instant": 2, "resolution": 1}


# ---------------------------------------------------------------------------
# Bps statistics — odd count (median is the middle element)
# ---------------------------------------------------------------------------


def test_bps_stats_odd_count() -> None:
    # sorted bps: 100, 200, 300  → median = 200, mean = 200, min = 100, max = 300
    opps = [
        _opp(net_profit_bps="300"),
        _opp(net_profit_bps="100"),
        _opp(net_profit_bps="200"),
    ]
    s = summarize(opps)
    assert s.net_bps_min == Decimal("100")
    assert s.net_bps_median == Decimal("200")
    assert s.net_bps_max == Decimal("300")
    assert s.net_bps_mean == Decimal("200")


# ---------------------------------------------------------------------------
# Bps statistics — even count (median = avg of two middles)
# ---------------------------------------------------------------------------


def test_bps_stats_even_count() -> None:
    # sorted bps: 100, 200, 300, 400  → median = (200+300)/2 = 250, mean = 250
    opps = [
        _opp(net_profit_bps="400"),
        _opp(net_profit_bps="100"),
        _opp(net_profit_bps="300"),
        _opp(net_profit_bps="200"),
    ]
    s = summarize(opps)
    assert s.net_bps_min == Decimal("100")
    assert s.net_bps_median == Decimal("250")
    assert s.net_bps_max == Decimal("400")
    assert s.net_bps_mean == Decimal("250")


# ---------------------------------------------------------------------------
# P&L and notional
# ---------------------------------------------------------------------------


def test_total_would_be_pnl() -> None:
    # opp A: net_profit=0.10 * size=100 = 10
    # opp B: net_profit=0.20 * size=50  = 10
    # total = 20
    opps = [
        _opp(net_profit_bps="100", net_profit="0.10", executable_size="100"),
        _opp(net_profit_bps="200", net_profit="0.20", executable_size="50"),
    ]
    s = summarize(opps)
    assert s.total_would_be_pnl == Decimal("20.00")


def test_directional_ev_split_from_structural_pnl() -> None:
    # A PARTIAL_BASKET opp's P&L is a directional, optimistic EV — it must land in directional_ev,
    # never inflate the structural total_would_be_pnl headline.
    structural = _opp(
        detector=DetectorKind.NEGRISK_BASKET,
        net_profit_bps="100",
        net_profit="0.10",
        executable_size="100",
        realizes="resolution",
    )  # $10
    partial = _opp(
        detector=DetectorKind.PARTIAL_BASKET,
        net_profit_bps="50",
        net_profit="0.05",
        executable_size="100",
        realizes="resolution",
    )  # $5
    s = summarize([structural, partial])
    assert s.total_would_be_pnl == Decimal("10.00")  # structural only
    assert s.directional_ev == Decimal("5.00")  # partial separated out


def test_total_executable_notional() -> None:
    # opp A: cost=0.90 * size=100 = 90
    # opp B: cost=0.50 * size=200 = 100
    # total = 190
    opps = [
        _opp(net_profit_bps="100", cost="0.90", executable_size="100"),
        _opp(net_profit_bps="200", cost="0.50", executable_size="200"),
    ]
    s = summarize(opps)
    assert s.total_executable_notional == Decimal("190.00")


# ---------------------------------------------------------------------------
# avg_days_to_resolution
# ---------------------------------------------------------------------------


def test_avg_days_averages_only_opps_with_days() -> None:
    # Only the two with days_to_resolution contribute: (10 + 20) / 2 = 15
    opps = [
        _opp(net_profit_bps="100", days_to_resolution=10),
        _opp(net_profit_bps="200", days_to_resolution=20),
        _opp(net_profit_bps="300", days_to_resolution=None),
    ]
    s = summarize(opps)
    assert s.avg_days_to_resolution == pytest.approx(15.0)


def test_avg_days_none_when_no_opp_has_days() -> None:
    opps = [
        _opp(net_profit_bps="100", days_to_resolution=None),
        _opp(net_profit_bps="200", days_to_resolution=None),
    ]
    assert summarize(opps).avg_days_to_resolution is None


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------


def test_format_summary_returns_nonempty_string_with_total() -> None:
    opps = [_opp(net_profit_bps="500")]
    result = format_summary(summarize(opps))
    assert isinstance(result, str)
    assert len(result) > 0
    assert "1" in result  # total appears somewhere


def test_format_summary_empty_input() -> None:
    result = format_summary(summarize([]))
    assert isinstance(result, str)
    assert "0" in result


def test_format_summary_contains_detector_name() -> None:
    opps = [_opp(detector=DetectorKind.NEGRISK_BASKET, net_profit_bps="300")]
    result = format_summary(summarize(opps))
    assert "negrisk_basket" in result


# ---------------------------------------------------------------------------
# E1-d — realized-outcome ledger summary
# ---------------------------------------------------------------------------


def _entry(status: str, realized_pnl: str | None) -> LedgerEntry:
    return LedgerEntry(
        fingerprint=f"fp{status}{realized_pnl}",
        opp=_opp(net_profit_bps="30"),
        status=status,
        detection_count=1,
        first_detected_at="2026-07-01T00:00:00+00:00",
        last_detected_at="2026-07-01T00:00:00+00:00",
        realized_pnl=None if realized_pnl is None else Decimal(realized_pnl),
    )


def test_summarize_ledger_empty() -> None:
    s = summarize_ledger([])
    assert s.total_events == 0 and s.settled == 0 and s.realized_pnl == Decimal(0)
    assert "no economic events" in format_ledger_summary(s)


def test_summarize_ledger_mixes_pending_win_loss_void() -> None:
    entries = [
        _entry("pending", None),
        _entry("resolved", "10"),  # win
        _entry("resolved", "-5"),  # loss
        _entry("void", "-3"),  # loss, and a void
    ]
    s = summarize_ledger(entries)
    assert s.total_events == 4
    assert s.pending == 1
    assert s.settled == 3
    assert s.void == 1
    assert s.wins == 1
    assert s.losses == 2
    assert s.realized_pnl == Decimal("2")  # 10 - 5 - 3
    assert s.worst_loss == Decimal("-5")


def test_format_ledger_flags_negative_settlement() -> None:
    s = summarize_ledger([_entry("void", "-5")])
    out = format_ledger_summary(s)
    assert "worst realized loss" in out
    assert "settled negative" in out  # the E2 hook is called out for the user


# --- rec #3: shadow-floor arrival-rate summary ---


def _shadow_entry(fp: str, first: str, bps: str, cost: str, size: str) -> LedgerEntry:
    opp = _opp(net_profit_bps=bps, cost=cost, executable_size=size)
    return LedgerEntry(
        fingerprint=fp,
        opp=opp,
        status="pending",
        detection_count=1,
        first_detected_at=first,
        last_detected_at=first,
    )


def test_summarize_shadow_empty_is_none() -> None:
    assert summarize_shadow_arrivals([]) is None
    assert "not running" in format_shadow_summary(None)


def test_summarize_shadow_arrival_rate_over_multi_day_span() -> None:
    entries = [
        _shadow_entry("a", "2026-07-01T00:00:00+00:00", "40", "0.90", "10"),
        _shadow_entry("b", "2026-07-03T00:00:00+00:00", "60", "0.80", "5"),
    ]  # 2 distinct over a 2-day span → 1.0/day
    s = summarize_shadow_arrivals(entries)
    assert s is not None
    assert s.distinct == 2
    assert s.span_days == 2.0
    assert s.per_day == 1.0
    assert s.bps_min == Decimal("40") and s.bps_max == Decimal("60")
    assert "1.00 distinct/day" in format_shadow_summary(s)


def test_summarize_shadow_short_window_withholds_rate() -> None:
    entries = [
        _shadow_entry("a", "2026-07-01T00:00:00+00:00", "40", "0.90", "10"),
        _shadow_entry("b", "2026-07-01T00:10:00+00:00", "50", "0.90", "10"),
    ]  # 10-min span → too short to quote a rate
    s = summarize_shadow_arrivals(entries)
    assert s is not None and s.per_day is None
    assert "need >=" in format_shadow_summary(s)


# --- deduped per-opp listing (`ledger` command) ---


def test_format_ledger_lines_one_line_per_distinct_event() -> None:
    from polyarb.engine.backtest import format_ledger_lines

    entries = [
        _shadow_entry("a", "2026-07-01T00:00:00+00:00", "75.8", "0.977", "10"),
        _shadow_entry("b", "2026-07-01T00:00:00+00:00", "40.0", "0.90", "5"),
    ]
    entries[0] = LedgerEntry(**{**entries[0].__dict__, "detection_count": 342})
    out = format_ledger_lines(entries)
    assert len(out.splitlines()) == 2  # 2 distinct events, not 342+5 rows
    assert "x342" in out  # the re-detection count is surfaced, not repeated lines
    assert "75.8bps" in out


def test_format_ledger_lines_empty() -> None:
    from polyarb.engine.backtest import format_ledger_lines

    assert "no economic events" in format_ledger_lines([])
