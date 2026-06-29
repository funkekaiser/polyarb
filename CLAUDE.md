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

> Status: **Phases 1–4 complete** — read-only clients + models, the three detectors with
> property-tested math, the dependency ladders/DAGs (RELATIONS.md), a working
> `scan --dry-run` (engine/filters/ranking/sinks), and hardening/analytics (Docker,
> `backtest`/`replay`, graceful shutdown, structured logs). Remaining: **Phase 5** —
> execution module (scaffold only, default OFF).

## Doc map — one home per fact (avoid drift)

- **`SPEC.md`** — design source of truth: the math, the non-negotiable constraints, the
  stack, the repo layout, and the phase plan with review gates.
- **`CLAUDE.md`** (this file) — session rules, commands, and the architecture mental model.
- **`docs/API_NOTES.md`** — live-verified API facts (base URLs, quotas, fees, real-payload
  quirks), dated. The build references this, not memory.
- **`docs/RELATIONS.md`** — design spec for the logical-dependency subsystem (ladders vs
  declared DAGs, tag schema, seed relations, resolution-fingerprint gate). Mostly Phase 3.
- **`docs/TESTING.md`** — how correctness is defined, the test-suite map, the adversarial
  bug-hunt findings + fixes, known limitations, and where bugs are likely to hide.

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

## Operating model — orchestrate, don't grind

You are the **orchestrator**, not the line worker. Your scarcest resource is the *big
picture* — the SPEC math, the cross-cutting invariants, the plan in your head — not tokens.
Spend it on judgment; delegate the grind. A turn should look like: **plan → delegate scoped
pieces (parallel where independent) → verify → synthesize → decide.** You stay at the seams;
subagents do the volume.

- **Default to delegation.** Before doing a task yourself, ask: *could a subagent do this from
  a crisp spec?* If yes, write the spec and delegate. The act of writing the spec externalizes
  the big picture — which is exactly what makes it safe to hand off.
- **Code it yourself only when** you are the *sole* holder of the picture **and** the work is
  too tightly coupled to specify without basically doing it — or it's a trivial one-liner.
  "I understand it best" is not a reason to type it; it's a reason to write the spec.
- **Right model for the job** (pick deliberately, per call):
  - **Opus (you):** planning, cross-cutting design, the profit-math/SPEC reasoning, spec and
    design-doc authoring, final review, synthesis, judgment calls.
  - **Sonnet:** scoped implementation from a spec (a detector, a client, a bounded refactor),
    bug-hunts, code review, multi-file search.
  - **Haiku:** cheap mechanical sweeps — grep/format/rename, fixture munging, simple lookups.
- **Parallelize.** Fan out independent work in one message (multiple `Agent` calls). Let each
  subagent absorb the tool-output noise (file dumps, search hits, test logs) and return only
  the conclusion, so your context stays clean.
- **Never trust a "done" — verify.** Delegated output is a *proposal* until you've run the gate
  (`ruff` + `mypy` + `pytest`) or had an adversarial reviewer (often a separate subagent) try
  to break it. This repo earns correctness through adversarial verification (docs/TESTING.md);
  hold delegated work to that bar. The final correctness call is always yours.
- **Convene a review panel for high-stakes soundness calls.** The gate proves the code *runs*;
  it cannot prove a *strategy* is sound. For anything load-bearing on the profit math, the
  detectors, the fee/sizing/gas model, or any new arb identity, fan out a **panel of
  independent Opus reviewers, each with a distinct adversarial lens** (e.g. profit-identity
  math · market-microstructure/execution realism · numerical-implementation fidelity), run
  them in parallel, then **you synthesize** their findings into one ranked critique and feed
  the durable strategy-level concerns into `docs/STRATEGY_BACKLOG.md`. This is the
  "statistician committee" pattern that has already surfaced real issues (see TESTING.md /
  STRATEGY_BACKLOG.md). Reach for it when correctness is statistical rather than mechanical and
  a single reviewer's blind spots are unacceptable — not for routine diffs. Diversity of lens
  beats more reviewers of the same lens; pair it with the per-finding adversarial verify above.
- **Caveat:** subagents sharing this repo's venv can re-trigger the macOS `.pth` hidden-flag
  issue (see "venv" below). Tell file-only agents not to run `uv sync`/`uv run`, or give a
  mutating agent `isolation: "worktree"`.

## Workflow: phased, with review gates

Work the phases in `SPEC.md` **in order, one at a time**. At the end of each phase: run
`ruff check` + `ruff format` + `mypy src` + `pytest`, fix failures, commit (Conventional
Commits), then **STOP** and summarize (what was built, what the tests prove, decisions made)
and wait for the user's go-ahead. Do not start the next phase unprompted.

## Commands

```bash
uv sync --dev                                    # RUN FIRST each session / after dep changes

# Works today
uv run polyarb version                          # smoke check
uv run polyarb scan --dry-run                   # read-only ranked opportunity feed (default)
uv run polyarb record [--out DIR]               # capture live (read-only) samples → fixtures
uv run polyarb backtest                         # summarize stored opportunity history
uv run polyarb replay                           # print stored opportunities oldest-first
uv run pytest                                    # full suite (offline, fixture-based)
uv run pytest tests/test_models.py::test_name    # a single test
uv run ruff check . && uv run ruff format .      # lint + format
uv run mypy src                                  # strict type check

docker compose -f docker/docker-compose.yml up --build   # containerized long-running scanner
```

### venv: the macOS hidden-`.pth` problem and the layered fix

**Root cause of the recurring `ModuleNotFoundError: No module named 'polyarb'`.** On macOS,
`uv run` re-applies the BSD `UF_HIDDEN` flag to the installed `polyarb.pth` (the editable-
install path file), and **Python 3.12's `site.addpackage` silently skips hidden `.pth`
files**. So `src/` never lands on `sys.path` and the import fails — repeatedly, because it
re-hides on the next `uv run`. This is *not* the rename and *not* link-mode (it happens under
both `copy` and `hardlink`); diagnose with `ls -lO .venv/lib/python3.12/site-packages/*.pth`
(look for the `hidden` flag).

The fix makes imports **independent of the `.pth`** so the hidden flag stops mattering:

- **Tests** — `pyproject.toml` sets `pythonpath = ["src"]`, so `pytest` finds `polyarb`
  regardless of the `.pth`. (Robust; needs nothing else.)
- **CLI / `uv run python` / scripts** — `.claude/settings.json` sets `PYTHONPATH=src`
  (honored before site processing). `scripts/demo.py` also self-bootstraps `src/` onto the
  path. Note: a `settings.json` env change only takes effect **next** session — within the
  session that set it, prefix commands with `PYTHONPATH=src` manually.
- **Auto-sync race** — `UV_NO_SYNC=1` (same file) keeps `uv run` from rebuilding the editable
  install mid-run; with auto-sync off there's no concurrent-rebuild race.
- **One-shot rescue** if you still hit it: `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`.

`uv.toml` pins `link-mode = "hardlink"` (not `copy`) — not as the cure, but because the venv
`.pth` then shares the uv cache inode, so a single `chflags` on either clears both.

Consequence: **the venv does not self-heal.** Run `uv sync --dev` yourself at session start
and after any dependency change. Hard recovery: `rm -rf .venv && uv sync --dev`. CI is
unaffected (fresh Linux installs — no `UF_HIDDEN`).

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
