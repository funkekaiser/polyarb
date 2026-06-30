"""Optional Prometheus metrics for the scanner (off unless ``METRICS_ENABLED``).

The counters are always defined (cheap, no server); the ``/metrics`` HTTP endpoint only
starts when enabled, so a long-running containerised scanner can drop into an existing
Prometheus stack (SPEC marks this optional).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, start_http_server

SCAN_PASSES = Counter("polyarb_scan_passes_total", "Completed scan passes")
SCAN_ERRORS = Counter("polyarb_scan_errors_total", "Scan passes that raised an exception")
CANDIDATES = Counter(
    "polyarb_candidate_opportunities_total",
    "Candidate opportunities seen, before filtering",
    ["detector"],
)
EMITTED = Counter("polyarb_emitted_opportunities_total", "Opportunities emitted after filters")

# D7-heartbeat: monotonically-set gauge tracking when the loop last pulsed (Unix epoch
# seconds). Always defined (cheap, no server needed) so it is visible via /metrics when
# METRICS_ENABLED=true. Complements the heartbeat file: the file is read by the Docker
# HEALTHCHECK; this gauge is scraped by Prometheus.
LAST_PASS = Gauge(
    "polyarb_last_pass_timestamp_seconds",
    "Unix time of the last completed scan pass (success or error)",
)

# --- WebSocket streaming observability (R8) -------------------------------------------------
# These make the now-default streaming path observable: a connected-but-silent feed, reconnect
# churn, and resync health are otherwise invisible (the scan loop keeps pulsing off the cache).

WS_LAST_MESSAGE = Gauge(
    "polyarb_ws_last_message_timestamp_seconds",
    "Unix time the streaming runner last applied a WS message or successful resync",
)
WS_RECONNECTS = Counter("polyarb_ws_reconnects_total", "WS (re)connection attempts")
WS_STALLS = Counter(
    "polyarb_ws_stalls_total",
    "WS connections force-dropped by the stall watchdog (connected but silent)",
)
WS_RESYNCS = Counter("polyarb_ws_resyncs_total", "REST resync passes (stale-drain or full)")
WS_RESYNC_ERRORS = Counter(
    "polyarb_ws_resync_errors_total", "Individual REST resync fetches that failed"
)
WS_TOKENS = Gauge("polyarb_ws_tracked_tokens", "Tokens currently materialised in the book cache")
WS_SKIPS = Gauge(
    "polyarb_ws_skipped_entries_total",
    "Cumulative malformed/unparseable WS delta entries skipped by the cache",
)


def start_metrics_server(port: int) -> None:
    """Start the Prometheus ``/metrics`` HTTP endpoint on ``port`` (background thread)."""
    start_http_server(port)
