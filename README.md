# polyarb

Read-only **Polymarket structural-arbitrage scanner**. It continuously scans for
opportunities where prices violate a mathematical identity (not a forecasting opinion),
scores them net-of-fees, ranks by risk-adjusted/annualized return, persists, and alerts.

**Detection is the product.** Execution is a separate, opt-in, default-OFF module.

> Status: Phases 0–4 complete; Phase 5 (execution) not started. See `SPEC.md` for the full
> design, the math, and the phased plan; `docs/API_NOTES.md` for verified live-API facts;
> `CLAUDE.md` for the working rules.

## Structural edges detected

1. **Complement** — within a binary market, `YES + NO ≠ 1` (realizes instantly via merge/split).
2. **NegRisk basket** — within an N≥3 mutually-exclusive event, `Σ YES ≠ 1` (realizes at resolution).
3. **Logical dependency** — across linked markets, `A ⇒ B` so `P(A) ≤ P(B)` is violated.
4. **Cross-venue** — *stub only* (resolution-equivalence + jurisdiction caveats).

## Hard rules

- Read-only by default. No order is signed/posted/cancelled and no private key is touched
  unless `EXECUTION_ENABLED=true` **and** a human confirms at runtime.
- No secrets in the repo — `.env.example` holds placeholders only; load real values via env.
- Verify the API against live docs, never from memory. Keep `docs/API_NOTES.md` current.

## Quick start

```bash
uv sync --dev                 # install (pins Python 3.12)
uv run polyarb version        # smoke check
uv run pytest                 # offline test suite
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

## Commands

These all ship and work today (read-only; no signing client, no credentials needed):

```bash
uv run polyarb scan --dry-run   # default, read-only ranked opportunity feed
uv run polyarb record           # capture live samples → test fixtures
uv run polyarb backtest         # analyze stored opportunities
```

Order placement (Phase 5) is not yet built.

## Run with Docker

The scanner ships as a self-contained image. SQLite history persists in a named volume so
it survives container restarts and image rebuilds.

```bash
# Build and start the long-running scanner (read-only, scan --dry-run by default).
docker compose -f docker/docker-compose.yml up --build

# One-shot commands — container exits when the command finishes.
docker compose -f docker/docker-compose.yml run --rm scan record
docker compose -f docker/docker-compose.yml run --rm scan backtest
docker compose -f docker/docker-compose.yml run --rm scan version
```

The SQLite database is stored at `/data/polyarb.db` inside the container, backed by the
`polyarb-data` named volume. To wipe historical data: `docker compose -f docker/docker-compose.yml down -v` (destructive).

**Read-only by default in Docker.** The image runs `scan --dry-run`; no order is ever
signed or posted. `EXECUTION_ENABLED` defaults to `false` and is never set inside the
image — see the Configuration section below.

## Configuration

All configuration is loaded from environment variables (or a git-ignored `.env` file copied
from `.env.example`). No secrets are baked into the image.

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `MIN_PROFIT_BPS` | `30` | Minimum net-of-fees profit in basis points before an opportunity is reported. |
| `MIN_NOTIONAL_USDC` | `50` | Minimum executable size in USDC; opportunities below this are discarded. |
| `EXCLUDE_AT_RISK_RESOLUTION` | `true` | Skip opportunities where resolution source is flagged as high-risk. |
| `SCAN_INTERVAL_SECONDS` | `5` | Seconds between scanner loop iterations. |
| `NOTIFIER` | `none` | Alert sink: `none`, `webhook`, `ntfy`, `discord`, or `telegram`. |
| `NOTIFIER_URL` | _(empty)_ | Webhook/ntfy/Discord/Telegram URL when `NOTIFIER` is not `none`. |
| `SQLITE_PATH` | `polyarb.db` | Path to the SQLite database file (in Docker: `/data/polyarb.db`). |
| `METRICS_ENABLED` | `false` | Expose a Prometheus `/metrics` endpoint for the scanner. |
| `METRICS_PORT` | `9090` | Port for the `/metrics` endpoint when `METRICS_ENABLED=true`. |
| `EXECUTION_ENABLED` | `false` | **Leave `false`.** Must be `true` **and** confirmed at runtime to place any order. |

Real values (especially `POLYMARKET_PRIVATE_KEY` when execution is eventually enabled) must
come from the environment or a local `.env` file — never committed to the repository.

## Disclaimer

Engineering guidance only, not financial advice. Whether to ever enable execution — and
the ToS, jurisdiction, and tax questions that come with trading — is a separate decision
to be made with appropriate professional advice.
