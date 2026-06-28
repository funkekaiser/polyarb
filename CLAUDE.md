# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`polyarb` — a read-only Polymarket **structural-arbitrage scanner**. It continuously
scans for opportunities where prices violate a mathematical identity (not a forecasting
opinion), scores them net-of-fees, ranks by risk-adjusted/annualized return, persists,
and alerts. **Detection is the product.** Execution is a separate, deliberately-OFF
module. The authoritative design document is `SPEC.md` — read it for the math, the
repository layout, and the full phase plan before doing substantive work.

> Status note: the repo is currently a spec-only scaffold (no code committed yet). The
> directory layout and commands below describe the target shape defined in `SPEC.md`.
> Until a file exists, treat its path here as the agreed destination, not a claim that it
> is present.

## Hard rules (non-negotiable — see SPEC.md §"Non-negotiable constraints")

- **Read-only by default.** No order is ever created, signed, posted, or cancelled, and
  no private key is touched, unless `EXECUTION_ENABLED=true` AND a human confirms at
  runtime. The default `scan` path must not even instantiate a signing client. Gamma and
  Data need no auth; CLOB **book reads** are public; only **trading** needs credentials.
- **Verify the live API — never from memory.** Before implementing or changing any
  client, fetch and read the current docs (`docs.polymarket.com`, the
  `Polymarket/py-clob-client` README). Confirm endpoints, field names, auth, rate limits,
  the condition_id ↔ token_id identifier model, the settlement token (USDC vs pUSD), and
  split/merge + NegRisk-convert mechanics. Keep `docs/API_NOTES.md` current and dated; the
  rest of the build references that file, not assumptions.
- **No secrets in the repo, ever.** `.env.example` holds placeholders only. Real values
  come from a git-ignored `.env` / the environment, loaded via `pydantic-settings`. A
  private key is read from env only at execution time and is never logged, printed, or
  written to disk.
- **Respect rate limits.** Per-service token-bucket limiter (Gamma, CLOB, Data have
  different quotas; the websocket caps concurrent connections — confirm exact numbers from
  live docs). Exponential backoff with jitter on HTTP 429 / Cloudflare throttling. Prefer
  the websocket for live book updates; cache Gamma metadata.
- **Resolution risk is a first-class filter.** Markets resolve via the UMA oracle and can
  be disputed/manipulated. Every emitted opportunity carries a `resolution_risk` tag; the
  default filter hard-excludes at-risk markets.
- **NegRisk convert ≠ arbitrage.** Convert is a capital-efficiency tool (pay 1, get 1, no
  profit). Profit comes from buying the underpriced basket through the standard order
  books. This distinction must live in code, a docstring, and a test.
- **Tests never hit the live API.** Unit/detector tests run against committed JSON
  fixtures. Only `scan --dry-run` (and the explicit `record` command) touch the live
  read-only API.

## Phased build with review gates

Work the phases in `SPEC.md` **in order, one at a time**. At the end of *each* phase:
run lint + format + type-check + tests, fix failures, commit with a Conventional Commits
message, then **STOP** and post a short summary (what was built, what the tests prove, any
decisions made) and wait for the user's go-ahead. Do not start the next phase
unprompted. Phases: 0 Bootstrap & verify → 1 Clients & models → 2 Detectors, pricing,
property tests → 3 Engine, filters, sinks (working dry-run scanner) → 4 Hardening,
container, analytics → 5 Execution module (scaffold only, leave OFF).

## Stack

Python 3.12 / `uv` / `httpx` (async REST) / `websockets` / `pydantic` v2 /
`pydantic-settings` / `polymarket-client` (execution module only — replaces the now-archived
`py-clob-client`; see `docs/API_NOTES.md`) /
SQLite (storage behind an interface so Postgres is a drop-in) / `structlog` /
`pytest` + `hypothesis` / `ruff` (lint+format) + `mypy` (strict) / Docker + Compose /
GitHub Actions CI / pluggable notifier (webhook/ntfy/Discord/Telegram, off unless
configured).

## Commands

```bash
uv run python -m polyarb.cli scan --dry-run    # default, read-only opportunity feed
uv run python -m polyarb.cli record            # capture live samples → test fixtures
uv run python -m polyarb.cli backtest          # analyze stored opportunities
uv run python -m polyarb.cli replay            # replay stored opportunities

uv run pytest                                  # full test suite (offline, fixture-based)
uv run pytest tests/test_negrisk.py            # one test file
uv run pytest tests/test_negrisk.py::test_name # one test
uv run ruff check && uv run ruff format        # lint + format
uv run mypy src                                # strict type check

docker compose -f docker/docker-compose.yml up # long-running scan service
```

## Architecture (target shape — see SPEC.md for full tree)

Pipeline, end to end: **discover → read books → detect → filter → rank → emit**.

- `clients/` — talk to Polymarket. `gamma.py` discovers events/markets and negRisk flags
  (public); `clob.py` does public reads (order books, prices, midpoints, fees, tick/min
  size); `data.py` reads positions/trades/holders (public); `ws.py` streams live book
  updates; `ratelimit.py` is the shared per-service token bucket + backoff.
- `models.py` — typed pydantic domain: Event, Market, Outcome, OrderBook, Opportunity.
- `detectors/` — each implements the `base.py` Detector protocol → `Iterable[Opportunity]`.
  Three real detectors (`complement`, `negrisk_basket`, `dependency`) plus a `crossvenue`
  **stub** that raises `NotImplementedError` and gates on `resolution_equivalence_check()`.
  The exact profit math for each lives in SPEC.md §"The math" and must be property-tested.
- `pricing/` — `fees.py` (per-market/category live fee model → net profit) and
  `sizing.py` (executable size from cumulative book depth; reject opps below MIN_NOTIONAL).
- `resolution/` — `risk.py` classifies a resolution source into a risk tag; `relations.py`
  is the **hand-declared** logical-dependency graph (A ⇒ B ⇒ P(A) ≤ P(B)); adding a
  relation must be a one-liner. Dependencies are declared, never inferred from text.
- `engine/` — `scanner.py` is the async fetch→detect→filter→rank→emit loop; `filters.py`
  (fee/size/resolution/dedupe-cooldown); `ranking.py` (net_profit, annualized, risk).
- `sinks/` — `store.py` (SQLite persistence behind an interface) and `notify.py`
  (pluggable, optional notifier).
- `execution/` — **GATED, default OFF.** `guard.py` enforces `EXECUTION_ENABLED`, a
  max-notional cap, a kill-switch, and per-trade interactive confirmation; `executor.py`
  does multi-leg submission via `py-clob-client` **only** through the guard. Nothing here
  runs on the default scan path.

Key cross-cutting invariants every change must preserve: read-only default; net-of-fees
profit (never gross); executable-size floor (never report a one-share opp); resolution-risk
gating; and the NegRisk convert-is-not-arb rule.
