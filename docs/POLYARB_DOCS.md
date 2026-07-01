# Polyarb — operating guide

How to run, deploy, and operate the read-only scanner. **Docker is the recommended,
long-term way to run polyarb**; a local/manual path is kept as a backup for development
(see [§Manual run (backup)](#manual-run-backup)).

> **Read-only by default.** The scanner never signs, posts, or cancels an order and never
> touches a private key. Execution is a separate, default-OFF module (Phase 5, not built).
> `EXECUTION_ENABLED` defaults to `false` and is **not** set inside the Docker image — do
> not change it without reading `SPEC.md` §"Non-negotiable constraints".

For *what* polyarb detects and *why* the numbers are trustworthy, see `README.md` (overview)
and `SPEC.md` (the math + design). This doc is purely operational.

---

## Run with Docker (recommended)

```bash
# Build the image and start the long-running scanner in the background:
make docker-up        # → docker compose -f docker/docker-compose.yml up --build -d

# Tail the structured logs (Ctrl-C stops tailing; the container keeps running):
make docker-logs      # → docker compose … logs -f

# Stop the container (the SQLite data volume is preserved):
make docker-down      # → docker compose … down
```

That's the whole loop. The container starts on `scan --dry-run` (a continuous read-only
scan), restarts automatically unless you stop it, and persists everything it finds to a
named volume that survives restarts and rebuilds.

Configuration is read from a git-ignored `.env` in the repo root (copy from `.env.example`).
The image ships safe defaults for every variable, so `.env` is optional — see
[§Configuration](#configuration).

**One-shot commands** (run a single command in a throwaway container, then exit):

```bash
docker compose -f docker/docker-compose.yml run --rm scan backtest   # summarize stored history
docker compose -f docker/docker-compose.yml run --rm scan replay     # oldest-first feed
docker compose -f docker/docker-compose.yml run --rm scan record     # capture live fixtures
docker compose -f docker/docker-compose.yml run --rm scan version    # smoke check
```

---

## How the Docker container works

The setup is two files: `docker/Dockerfile` (the image) and `docker/docker-compose.yml`
(how it runs). Here's what each piece does and why.

### The image (`docker/Dockerfile`)

- **Multi-stage build.** Stage 1 installs dependencies with `uv sync --frozen --no-dev` into
  `/app/.venv`; stage 2 is a clean `python:3.12-slim` runtime that copies only the built venv
  and `src/` — no build tools, no dev deps, small surface.
- **Non-root.** Runs as a system user `polyarb` (uid 1001); `/data` is the one writable
  directory it owns.
- **Entrypoint** is the installed `polyarb` binary; the default command is `scan --dry-run`.
  Override it by passing a different command (as the one-shot examples do).
- Being a clean Linux build, the image is **immune to the macOS `.pth` quirk** that affects
  local runs (see [§Troubleshooting](#troubleshooting-macos-import-error)).

### How it runs (`docker/docker-compose.yml`)

The compose project is named **`polyarb`**, so the container is **`polyarb-scan-1`** and the
image is **`polyarb:latest`** — use those names with `docker exec` / `docker logs`.

- **Persistence.** A named volume **`polyarb-data`** is mounted at `/data`, and `SQLITE_PATH`
  is pinned to `/data/polyarb.db`. History survives `docker compose down` and image rebuilds;
  only `down -v` deletes it (destructive). See [§Where results go](#where-results-go).
- **Restart policy.** `restart: unless-stopped` — it comes back after a crash or a host
  reboot, but stays down once you `make docker-down`.
- **Graceful shutdown.** `stop_grace_period: 30s` plus `init: true` (tini as PID 1) means a
  stop signal is forwarded cleanly; the scanner finishes its current pass and flushes SQLite
  before exiting.
- **Healthcheck.** `polyarb healthcheck` reads the **scan heartbeat** file (`HEARTBEAT_PATH`,
  written once per pass) and — because streaming is the default — the **WS heartbeat**
  (`WS_HEARTBEAT_PATH`, pulsed on each applied WS message or successful resync). It exits non-zero
  if either is stale, so it catches a *wedged scan loop* **and** a *frozen book cache* (WS + resync
  both stuck), not merely a dead process. `METRICS_ENABLED=true` still serves `/metrics` internally
  for Prometheus scraping (incl. the `ws_*` gauges), but liveness is the heartbeats, not `/metrics`.
- **Hardening.** `read_only` root filesystem (only the `/data` volume + a `/tmp` tmpfs are
  writable), `cap_drop: ALL`, `no-new-privileges`, and CPU/memory/pids ceilings
  (`deploy.resources.limits`). Fits the read-only product: the container can read the API and
  write its DB, nothing else.
- **Log rotation.** `json-file` driver capped at 10 MB × 5 files, so a long run can't fill the
  disk.

### Inspecting a running container

```bash
docker ps                              # status + health
docker logs -f polyarb-scan-1          # live JSON logs (same as make docker-logs)
docker exec polyarb-scan-1 /app/.venv/bin/polyarb backtest   # query the live DB
docker inspect polyarb-scan-1 --format '{{.State.Health.Status}}'
```

---

## Where results go

Opportunities are written to SQLite as they're detected.

| Mode | DB path | Notes |
|---|---|---|
| Docker | `/data/polyarb.db` | Backed by the `polyarb-data` named volume (persistent). |
| Local | `polyarb.db` (repo root, or `SQLITE_PATH`) | Created on first run. |

Read the stored history with the analytics commands (Docker one-shots shown; locally use
`make backtest` / `make replay`):

```bash
docker compose -f docker/docker-compose.yml run --rm scan backtest   # aggregate summary
docker compose -f docker/docker-compose.yml run --rm scan replay     # chronological feed
```

**Logs.** The scanner emits structured JSON to stdout (one object per line) — `timestamp`,
`level`, `event`, plus per-opportunity fields. `make docker-logs` tails them. Set
`LOG_LEVEL=DEBUG` for per-leg book reads, rate-limit events, and filter decisions.

---

## Configuration

All settings come from environment variables (or a git-ignored `.env`). No secrets are baked
into the image. The source of truth is `src/polyarb/config.py`.

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `SCAN_INTERVAL_SECONDS` | `5` | Seconds between scanner loop iterations. |
| `MIN_PROFIT_BPS` | `30` | Minimum net-of-fees profit (basis points) to report an opportunity. |
| `MIN_NOTIONAL_USDC` | `50` | Minimum executable size (USDC) — smaller opportunities are discarded. |
| `GAS_ESTIMATE` | `0.02` | Conservative static gas ceiling per opportunity (USDC). Relayer gas is ~$0 real cost; this is a safety margin. See `docs/API_NOTES.md` §Gas. |
| `GAS_PER_LEG_ESTIMATE` | `0.05` | Per-leg component of the gas ceiling (USDC), scaled by leg count. Matters most for high-N NegRisk baskets. |
| `USE_DYNAMIC_GAS` | `false` | When `true`, fetch live gas each pass (Polygon Gas Station + CoinGecko POL/USD) instead of the static estimates; any oracle failure falls back to static. |
| `EXCLUDE_AT_RISK_RESOLUTION` | `true` | Drop opportunities whose resolution source is flagged high-risk. |
| `MAX_BOOK_AGE_S` | `900` | Drop order books whose CLOB timestamp is older than this (seconds). `0` disables the staleness gate. |
| `ENABLE_PARTIAL_BASKETS` | `false` | Opt-in: emit partial NegRisk baskets as tagged directional (non-structural) opportunities. |
| `NOTIFIER` | `none` | Alert sink. Implemented: `none`, `webhook` (raw opportunity JSON), `discord` (formatted embed → Discord incoming webhook). `ntfy`/`telegram` are not yet built and silently fall back to `none`. |
| `NOTIFIER_URL` | _(empty)_ | URL for the alert sink when `NOTIFIER` is `webhook` or `discord` (required for those). For `discord`, the channel's **Integrations → Webhooks** URL. |
| `SQLITE_PATH` | `polyarb.db` | SQLite DB path. Docker pins this to `/data/polyarb.db`. |
| `METRICS_ENABLED` | `false` | Expose a Prometheus `/metrics` endpoint. Docker sets this `true` for scraping (the container liveness probe is the heartbeat, not `/metrics`). |
| `METRICS_PORT` | `9090` | Port for `/metrics` when enabled. |
| `HEARTBEAT_PATH` | _(empty)_ | When set, `Scanner.run` writes the last-pass timestamp here each pass; `polyarb healthcheck` reads it (Docker's liveness probe). Docker sets `/data/polyarb-heartbeat`. Leave empty for local runs. |
| `STREAMING_ENABLED` | `true` | **WebSocket-first read path (the default).** Books are cached from the CLOB market channel and a candidate is REST-confirmed before emit; the REST poll is the resync/backup. Set `false` for pure REST polling. |
| `WS_RESYNC_INTERVAL_S` | `60` | Cadence of the full-depth REST resync (the backup/correction read). |
| `WS_STALL_TIMEOUT_S` | `60` | Force-reconnect a connected-but-silent feed after this many seconds (`0` disables). |
| `WS_FRESHNESS_S` | `90` | Detect only off books refreshed (delta/resync) within this window. Keep `>= WS_RESYNC_INTERVAL_S` + margin. `0` disables. |
| `WS_MAX_BACKOFF_S` | `30` | Cap on the exponential reconnect backoff. |
| `WS_MAX_MESSAGE_BYTES` | `67108864` | Max inbound WS frame (bytes). The initial-dump snapshot is one large frame; the library's 1 MiB default would drop it. See `docs/API_NOTES.md` §WebSocket. |
| `WS_HEARTBEAT_PATH` | _(empty)_ | When streaming, the runner pulses this on each WS message/resync; `polyarb healthcheck` requires it fresh (catches a frozen cache). Docker sets `/data/polyarb-ws-heartbeat`. |
| `EXECUTION_ENABLED` | `false` | **Leave `false`.** Execution is Phase 5 and not built. |

Example `.env`:

```bash
# .env (git-ignored; copy from .env.example)
LOG_LEVEL=DEBUG
MIN_PROFIT_BPS=20
NOTIFIER=discord
NOTIFIER_URL=https://discord.com/api/webhooks/<id>/<token>
```

> Real secrets (e.g. `POLYMARKET_PRIVATE_KEY`, only if execution is ever enabled) come from
> the environment at runtime — never committed, never baked into the image.

---

## Manual run (backup)

For development and quick checks. **Docker is the recommended path**; reach for this when you
want a fast local loop or are working on the code.

Prerequisites: [uv](https://docs.astral.sh/uv/getting-started/installation/), then bootstrap
the venv once per shell session:

```bash
make sync        # → uv sync --dev
```

```bash
make scan        # one pass, print ranked opportunities, exit   (uv run polyarb scan --passes 1)
make monitor     # continuous loop until interrupted            (uv run polyarb scan)
make backtest    # aggregate summary of stored opportunities
make replay      # chronological replay of the persisted feed
```

Stop a local run with **Ctrl-C** (SIGINT) or `kill -TERM <pid>`; the scanner finishes the
current pass, flushes SQLite, and exits cleanly.

> **macOS note:** local runs hit a venv quirk (a hidden `.pth` file); run `make sync` per
> session and see [§Troubleshooting](#troubleshooting-macos-import-error). The Docker
> image is unaffected — another reason it's the recommended path.

---

## Troubleshooting (macOS import error)

Symptom: `ModuleNotFoundError: No module named 'polyarb'` on local runs (never in Docker/CI).

**Root cause.** On macOS, `uv run` re-applies the BSD `UF_HIDDEN` flag to the installed
`polyarb.pth` (the editable-install path file), and **Python 3.12's `site.addpackage`
silently skips hidden `.pth` files**. So `src/` never lands on `sys.path` and the import
fails — repeatedly, because it re-hides on the next `uv run`. This is *not* the rename and
*not* link-mode (it happens under both `copy` and `hardlink`); diagnose with
`ls -lO .venv/lib/python3.12/site-packages/*.pth` (look for the `hidden` flag).

The fix makes imports **independent of the `.pth`** so the hidden flag stops mattering:

- **Tests** — `pyproject.toml` sets `pythonpath = ["src"]`, so `pytest` finds `polyarb`
  regardless of the `.pth`.
- **CLI / `uv run python` / scripts** — `.claude/settings.json` sets `PYTHONPATH=src`
  (honored before site processing). Note: a `settings.json` env change only takes effect
  **next** session — within the session that set it, prefix commands with `PYTHONPATH=src`.
- **Auto-sync race** — `UV_NO_SYNC=1` keeps `uv run` from rebuilding the editable install
  mid-run, so there's no concurrent-rebuild race.
- **One-shot rescue:** `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`.

`uv.toml` pins `link-mode = "hardlink"` so the venv `.pth` shares the uv cache inode and a
single `chflags` clears both.

Consequence: **the venv does not self-heal.** Run `uv sync --dev` yourself at session start
and after any dependency change. Hard recovery: `rm -rf .venv && uv sync --dev`. CI and Docker
are unaffected (fresh Linux installs — no `UF_HIDDEN`).
