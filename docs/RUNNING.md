# Running polyarb

Operator reference for running the read-only scanner locally or in a container.

> **Read-only by default.** The scanner never signs, posts, or cancels an order and
> never touches a private key. Execution is a separate, default-OFF module (Phase 5,
> not yet built). `EXECUTION_ENABLED` defaults to `false` and is not set inside the
> Docker image — do not change it without reading SPEC.md §"Non-negotiable constraints".

---

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — for local runs.
- Docker (any recent release) — for containerised runs.

Local runs additionally require a bootstrapped venv:

```bash
make sync        # → uv sync --dev
```

Run `make sync` once per shell session. On macOS there is a venv quirk involving a
hidden `.pth` file; see CLAUDE.md → "venv" for the full story. A clean Linux machine
(and the Docker image) are unaffected.

---

## Local — one pass

Run the full pipeline once, print ranked opportunities, then exit:

```bash
make scan
# expands to: uv run polyarb scan --passes 1
```

---

## Local — continuous monitor

Run the pipeline on a loop (default interval: 5 s) until interrupted:

```bash
make monitor
# expands to: uv run polyarb scan
```

Stop with **Ctrl-C** (SIGINT) or `kill -TERM <pid>`. The scanner handles both signals
gracefully: it finishes the current pass, flushes the SQLite write buffer, and exits
cleanly.

---

## Containerised — long-running scanner

The recommended production mode. The image is a clean Linux build so the macOS `.pth`
quirk does not apply.

```bash
# Build and start in the background:
make docker-up        # docker compose … up -d

# Tail logs (Ctrl-C stops tailing, leaves the container running):
make docker-logs      # docker compose … logs -f

# Stop the container (SQLite data volume is preserved):
make docker-down      # docker compose … down
```

The image default command is `scan --dry-run` (continuous loop). Configuration is loaded
from `.env` in the repo root (git-ignored; copy from `.env.example`). The image has safe
defaults for all variables, so `.env` is optional.

---

## Persistence

Opportunities are written to a SQLite database as they are detected.

| Mode | Default path | Notes |
|---|---|---|
| Local | `polyarb.db` (repo root, or `SQLITE_PATH`) | Created on first run. |
| Docker | `/data/polyarb.db` | Backed by the `polyarb-data` named volume. Survives container restarts and image rebuilds. |

The named volume is removed only by `docker compose … down -v` — that is destructive and
wipes all historical scan data.

Inspect the database with the bundled analytics commands:

```bash
make backtest    # aggregate summary of stored opportunities
make replay      # chronological replay of the persisted feed
```

Or with Docker (one-shot; container exits when the command finishes):

```bash
docker compose -f docker/docker-compose.yml run --rm scan backtest
docker compose -f docker/docker-compose.yml run --rm scan replay
```

---

## Configuration

All settings are loaded from environment variables (or a `.env` file in the repo root).
No secrets are baked into the image. The full source of truth is `src/polyarb/config.py`.

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `SCAN_INTERVAL_SECONDS` | `5` | Seconds between scanner loop iterations. |
| `MIN_PROFIT_BPS` | `30` | Minimum net-of-fees profit (basis points) to report an opportunity. |
| `MIN_NOTIONAL_USDC` | `50` | Minimum executable size (USDC) — smaller opportunities are discarded. |
| `GAS_ESTIMATE` | `0` | Fixed gas cost per opportunity (USDC). Set a real Polygon value to include gas in the net calculation. |
| `GAS_PER_LEG_ESTIMATE` | `0` | Per-leg gas cost (USDC). Scales with the number of legs; matters most for high-N NegRisk baskets. |
| `EXCLUDE_AT_RISK_RESOLUTION` | `true` | Drop opportunities where the resolution source is flagged high-risk. |
| `MAX_BOOK_AGE_S` | `900` | Drop order books whose CLOB timestamp is older than this (seconds). `0` disables the staleness gate. |
| `ENABLE_PARTIAL_BASKETS` | `false` | Opt-in: emit partial NegRisk baskets as tagged directional (non-structural) opportunities. Off by default. |
| `NOTIFIER` | `none` | Alert sink: `none`, `webhook`, `ntfy`, `discord`, or `telegram`. |
| `NOTIFIER_URL` | _(empty)_ | URL for the alert sink when `NOTIFIER` is not `none`. |
| `SQLITE_PATH` | `polyarb.db` | Path to the SQLite database. In Docker: `/data/polyarb.db`. |
| `METRICS_ENABLED` | `false` | Expose a Prometheus `/metrics` endpoint. |
| `METRICS_PORT` | `9090` | Port for the `/metrics` endpoint when `METRICS_ENABLED=true`. |
| `EXECUTION_ENABLED` | `false` | **Leave `false`.** Execution is Phase 5 and not yet built. |

Set variables in the environment or in a `.env` file:

```bash
# .env (git-ignored; copy from .env.example)
LOG_LEVEL=DEBUG
MIN_PROFIT_BPS=20
NOTIFIER=ntfy
NOTIFIER_URL=https://ntfy.sh/my-polyarb-topic
```

---

## Logs

The scanner emits structured JSON to stdout (one object per line). Each log line contains
at minimum `timestamp`, `level`, and `message`; opportunity events carry additional fields
(`strategy`, `profit_bps`, `executable_size_usdc`, etc.).

Tail logs for the containerised scanner:

```bash
make docker-logs
# or: docker compose -f docker/docker-compose.yml logs -f
```

Set `LOG_LEVEL=DEBUG` to see per-leg book reads, rate-limit events, and filter decisions.

---

## Stopping

| Mode | How to stop |
|---|---|
| Local (`make scan` / `make monitor`) | **Ctrl-C** (SIGINT) in the terminal, or `kill -TERM <pid>`. |
| Docker (`make docker-up`) | `make docker-down` (sends SIGTERM; waits for graceful shutdown). |

The scanner catches both SIGINT and SIGTERM, finishes the current pipeline pass, and exits.
