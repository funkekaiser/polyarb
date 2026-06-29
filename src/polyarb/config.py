"""Runtime configuration via pydantic-settings.

Loaded from the environment / a git-ignored ``.env`` (see ``.env.example``). The default
values are detection-only and read-only: execution stays off unless explicitly enabled.
No secrets are defined here; a private key (execution only) is read from env at use time.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- logging / scan loop ---
    log_level: str = "INFO"
    scan_interval_seconds: float = 5.0

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
    # Both default 0 (gas off) — set real Polygon numbers to enable. A single fixed charge
    # under-prices large baskets, so the per-leg term matters most for high-N opps.
    gas_estimate: Decimal = Decimal(0)
    gas_per_leg_estimate: Decimal = Decimal(0)
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


def load_settings() -> Settings:
    return Settings()
