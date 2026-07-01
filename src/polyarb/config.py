"""Runtime configuration via pydantic-settings.

Loaded from the environment / a git-ignored ``.env`` (see ``.env.example``). The default
values are detection-only and read-only: execution stays off unless explicitly enabled.
No secrets are defined here; a private key (execution only) is read from env at use time.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- logging / scan loop ---
    log_level: str = "INFO"
    scan_interval_seconds: float = 5.0
    # E1 — cadence for the read-only settlement poller inside the scanner loop: poll Gamma for the
    # resolution of pending ledger events at most this often (default 1h; 0 disables in-loop
    # settling — use the standalone `polyarb settle` command instead). Resolutions change slowly,
    # so a slow cadence keeps it off the latency-critical path.
    settle_interval_seconds: float = 3600.0

    # --- emission thresholds (the part that makes detection non-naive) ---
    min_profit_bps: Decimal = Decimal(30)
    min_notional_usdc: Decimal = Decimal(50)
    exclude_at_risk_resolution: bool = True
    dedupe_cooldown_seconds: float = 300.0
    # §5 — opt-in probabilistic partial basket (docs/HEDGING.md §5). OFF by default and never on
    # the default scan path: when the full NegRisk basket can't be locked (a leg is unbuyable),
    # emit the buyable subset as a *directional* (NOT structural / model-free) bet on the
    # market-implied residual, tagged DIRECTIONAL so it ranks below every structural arb.
    enable_partial_baskets: bool = False

    # --- discovery / fetch bounds ---
    event_discovery_limit: int = 200
    max_markets_per_scan: int = 80
    # Per-execution gas (USDC), applied once per opportunity, scaled by leg count (B2'):
    #   gas = gas_estimate (fixed: merge/redeem) + gas_per_leg_estimate · N (one taker fill/leg).
    # NOTE: via Polymarket's relayer (proxy/Safe/deposit wallets) gas is RELAYER-PAID — including
    # CTF split/merge/redeem — so the true user cost is ≈$0 (see docs/API_NOTES.md, dated).
    # DEFAULT = 0 (relayer reality; committee-confirmed 2026-07-01): the old 0.02/0.05 ceiling
    # over-charged raw Polygon gas the relayer user never pays and silently suppressed real small
    # multi-leg edges (e.g. ~104 bps on a 10-leg $50 basket) — the exact inventory the small-edge
    # strategy wants. To re-enable a conservative raw-EOA / relayer-cap ceiling, set these back to
    # ~0.02 / ~0.05; or flip use_dynamic_gas to price a live oracle (raw-EOA path only).
    gas_estimate: Decimal = Decimal("0")
    gas_per_leg_estimate: Decimal = Decimal("0")
    # When true, fetch live gas (Polygon Gas Station + POL/USD) each pass via GasClient and use
    # it instead of the static estimates above; on any oracle failure the scanner falls back to
    # the static values. Off by default — opt-in so the live dependency can't surprise a monitor.
    use_dynamic_gas: bool = False
    # A3 — staleness gate: drop order books whose CLOB last-change timestamp is older than this.
    # This is a *gross-staleness / corrupt-snapshot* net (the CLOB has served hours-old
    # 0.01/0.99 snapshots that would manufacture phantom arbs), NOT a fine freshness guarantee:
    # a book quiescent-but-valid (resting orders still executable) also has an old timestamp, so
    # too low a value drops genuine thin-market arbs as false negatives. Default is deliberately
    # generous; lower it to trade thin-market coverage for stricter staleness. 0 disables.
    max_book_age_s: float = 900.0

    # --- notifier (off unless configured) ---
    notifier: str = "none"  # none | webhook | ntfy | discord | telegram
    notifier_url: str | None = None

    # --- storage ---
    sqlite_path: str = "polyarb.db"

    # --- optional Prometheus /metrics endpoint (off by default) ---
    metrics_enabled: bool = False
    metrics_port: int = 9090

    # --- execution module (GATED — leave disabled) ---
    execution_enabled: bool = False
    max_trade_notional_usdc: Decimal = Decimal(0)

    # --- loop-progress liveness (D7-heartbeat) ---
    # When set, Scanner.run() atomically writes the epoch-seconds timestamp of the last pass
    # to this file after every scan_once attempt. The `polyarb healthcheck` subcommand reads
    # it and exits non-zero if the timestamp is stale (loop is wedged). Leave None (default)
    # for local / non-Docker runs — the heartbeat write is a no-op and the existing test
    # suite is completely unaffected.
    heartbeat_path: Path | None = None

    # --- websocket streaming (the DEFAULT architecture; REST is the resync/backup path) ---
    # WebSocket-first (Jonathan, 2026-07-01): books are maintained in-memory from the
    # market-channel websocket and a candidate detected off the cache is REST-confirmed before
    # emit (R1). The full-depth REST resync is the backup/correction path, not the primary read.
    # Set False only to fall back to the pure-REST poll loop (kept as a backup, not the default).
    streaming_enabled: bool = True
    # Cadence (seconds) of the full-depth REST resync that corrects any drift the top-of-book
    # WS integrity check can't catch (the phase-1 deep-drift mitigation / backup read).
    ws_resync_interval_s: float = 60.0
    # Cap (seconds) on the exponential reconnect backoff after a WS disconnect.
    ws_max_backoff_s: float = 30.0
    # Max inbound WS frame size (bytes). Polymarket sends the full initial-dump snapshot as one
    # frame that scales with subscribed-token count (~1.65 MiB for ~390 tokens); the websockets
    # library's 1 MiB default would close the connection (1009 MESSAGE_TOO_BIG) so the feed never
    # delivers a message. Default 64 MiB — generous headroom, still bounded so a rogue frame can't
    # OOM the process. (Verified live 2026-07-01.)
    ws_max_message_bytes: int = 64 * 1024 * 1024
    # R5 stall watchdog: force-drop + reconnect a connection that is OPEN (TCP/ping alive) but has
    # delivered no market message for this long. Across ~160 subscribed tokens real silence this
    # long means a dead feed, so a forced reconnect+resync is cheap insurance; set generously
    # above the WS ping cycle so a genuinely quiescent board is not churned. <=0 disables.
    ws_stall_timeout_s: float = 60.0
    # R2 streaming freshness guard: at detect time, ignore cached books not refreshed (delta or
    # resync) within this wall-clock window — distinct from max_book_age_s (the book's own
    # last-change time). A safety net atop R1's REST-confirm; bounds cross-leg skew at detect
    # time and stops wasting confirm round-trips on stale-cache phantoms. <=0 disables.
    # MUST be >= ws_resync_interval_s (+ a resync-drain margin): a quiescent token is restamped
    # only by the full resync, so a shorter window would make quiet-but-valid books blink out for
    # part of every cycle (committee finding). Default 90 = 60s resync + 30s margin.
    ws_freshness_s: float = 90.0
    # R8 stream-aware liveness: when set (and streaming_enabled), the runner atomically writes the
    # epoch-seconds of the last applied WS message OR successful resync here, and `polyarb
    # healthcheck` fails if it goes stale — so a wedged runner (both WS and resync stuck) can't
    # read healthy while the scan loop keeps pulsing off a frozen cache. None disables (local runs).
    ws_heartbeat_path: Path | None = None


def load_settings() -> Settings:
    return Settings()
