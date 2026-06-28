"""Smoke tests for the optional Prometheus metrics (no server started)."""

from __future__ import annotations

from prometheus_client import Counter

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
