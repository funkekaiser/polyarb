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

    # --- discovery / fetch bounds ---
    event_discovery_limit: int = 200
    max_markets_per_scan: int = 80
    gas_estimate: Decimal = Decimal(0)  # per-set round-trip gas estimate (USDC)

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
