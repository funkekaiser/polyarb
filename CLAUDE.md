# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`polyarb` — a read-only Polymarket **structural-arbitrage scanner**. It scans for prices
that violate a mathematical identity (not a forecasting opinion), scores them net-of-fees,
ranks, persists, and alerts. **Detection is the product;** execution is a separate,
default-OFF module.

**`SPEC.md` is the source of truth** — the math (profit identities), the non-negotiable
constraints, the tech stack, the repository layout, and the phased plan all live there. This
file is the *operational* guide for a coding session; it does not restate SPEC, it points to
it.

> Status: **Phase 1 complete** (read-only clients + domain models + fixtures). Next up:
> Phase 2 (detectors, pricing, property tests).

## Doc map — one home per fact (avoid drift)

- **`SPEC.md`** — design source of truth: the math, the non-negotiable constraints, the
  stack, the repo layout, and the phase plan with review gates.
- **`CLAUDE.md`** (this file) — session rules, commands, and the architecture mental model.
- **`docs/API_NOTES.md`** — live-verified API facts (base URLs, quotas, fees, real-payload
  quirks), dated. The build references this, not memory.

When a fact about constraints/stack/math/phases changes, edit **SPEC.md** (and API_NOTES if
it's an API fact). Don't copy it here.

## The one rule to never break

**Read-only by default.** No order is ever created, signed, posted, or cancelled, and no
private key is touched, unless `EXECUTION_ENABLED=true` **and** a human confirms at runtime.
The default scan path must not even instantiate a signing client. (Gamma/Data need no auth;
CLOB *book reads* are public; only *trading* needs credentials.)

The full constraint set — verify the live API before coding, no secrets in the repo, respect
rate limits with backoff, resolution-risk gating, NegRisk-convert-≠-arbitrage, tests never
hit the live API — is in **SPEC.md §"Non-negotiable constraints"**. Read it before
substantive work.

## Workflow: phased, with review gates

Work the phases in `SPEC.md` **in order, one at a time**. At the end of each phase: run
`ruff check` + `ruff format` + `mypy src` + `pytest`, fix failures, commit (Conventional
Commits), then **STOP** and summarize (what was built, what the tests prove, decisions made)
and wait for the user's go-ahead. Do not start the next phase unprompted.

## Commands

```bash
# Works today
uv run polyarb version                          # smoke check
uv run polyarb record [--out DIR]               # capture live (read-only) samples → fixtures
uv run pytest                                    # full suite (offline, fixture-based)
uv run pytest tests/test_models.py::test_name    # a single test
uv run ruff check . && uv run ruff format .      # lint + format
uv run mypy src                                  # strict type check

# Planned (later phases)
uv run polyarb scan --dry-run                    # Phase 3: read-only ranked opportunity feed
uv run polyarb backtest                          # Phase 4: analyze stored opportunities
```

If the editable install ever breaks (import of `polyarb` fails after a sync that changed the
package's files), recover with `rm -rf .venv && uv sync --dev`. CI is unaffected (fresh installs).

## Architecture (mental model — full tree is in SPEC.md)

Pipeline, end to end: **discover → read books → detect → filter → rank → emit**.

- `clients/` — Polymarket access. `gamma.py` (events/markets discovery), `clob.py` (public
  reads: books/prices/midpoints — **reads only**), `data.py` (trades/positions), `ws.py`
  (market-channel websocket), `ratelimit.py` (per-service token bucket + jittered backoff),
  `base.py` (shared async HTTP). *[Phase 1 — built.]*
- `models.py` — typed pydantic domain (Event, Market, Outcome, OrderBook; Opportunity comes
  with the detectors). Normalizes real-API quirks; see the module docstring + API_NOTES.
  *[Phase 1 — built.]*
- `detectors/` — each implements the `base.py` Detector protocol → `Iterable[Opportunity]`:
  `complement`, `negrisk_basket`, `dependency`, plus a `crossvenue` stub
  (`NotImplementedError` + `resolution_equivalence_check()`). Profit math is in
  SPEC.md §"The math" and must be property-tested. *[Phase 2.]*
- `pricing/` — `fees.py` (net-of-fees from live fee params) and `sizing.py` (executable size
  from cumulative book depth; reject opps below `MIN_NOTIONAL`). *[Phase 2.]*
- `resolution/` — `risk.py` (resolution-source → risk tag) and `relations.py` (hand-declared
  dependency graph; adding a relation is a one-liner; never inferred from text). *[Phase 2.]*
- `engine/` — `scanner.py` async fetch→detect→filter→rank→emit loop; `filters.py`;
  `ranking.py`. *[Phase 3.]*
- `sinks/` — `store.py` (SQLite behind an interface) and `notify.py` (optional). *[Phase 3.]*
- `execution/` — **GATED, default OFF.** `guard.py` (EXECUTION_ENABLED + max-notional cap +
  kill-switch + per-trade confirm); `executor.py` (multi-leg via `polymarket-client`, only
  through the guard). Never on the default scan path. *[Phase 5.]*

Cross-cutting invariants every change must preserve: read-only default; net-of-fees profit
(never gross); executable-size floor (never report a one-share opp); resolution-risk gating;
NegRisk-convert-is-not-arb.
