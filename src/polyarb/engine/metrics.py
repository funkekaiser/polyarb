"""Optional Prometheus metrics for the scanner (off unless ``METRICS_ENABLED``).

The counters are always defined (cheap, no server); the ``/metrics`` HTTP endpoint only
starts when enabled, so a long-running containerised scanner can drop into an existing
Prometheus stack (SPEC marks this optional).
"""

from __future__ import annotations

from prometheus_client import Counter, start_http_server

SCAN_PASSES = Counter("polyarb_scan_passes_total", "Completed scan passes")
SCAN_ERRORS = Counter("polyarb_scan_errors_total", "Scan passes that raised an exception")
CANDIDATES = Counter(
    "polyarb_candidate_opportunities_total",
    "Candidate opportunities seen, before filtering",
    ["detector"],
)
EMITTED = Counter("polyarb_emitted_opportunities_total", "Opportunities emitted after filters")


def start_metrics_server(port: int) -> None:
    """Start the Prometheus ``/metrics`` HTTP endpoint on ``port`` (background thread)."""
    start_http_server(port)
