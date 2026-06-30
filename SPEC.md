# Polymarket Structural-Arbitrage Scanner

## Mission

Build **`polyarb`**, a service that continuously scans Polymarket for **structural** arbitrage — opportunities that exist because prices violate a mathematical identity, not because we have a forecasting opinion. It detects, scores net-of-fees, ranks by risk-adjusted and annualized return, logs, and alerts.

**Detection is the deliverable.** A separate, opt-in, default-disabled module *may* place orders later, but the project is valuable and complete as a read-only detector. Build it that way.

> **Status (2026-06-30):** Phases 0–4 complete — the read-only scanner (clients + typed models, the three detectors with property-tested math incl. detector hardening, the dependency ladders/DAGs, engine/filters/ranking/sinks, Docker + analytics + the dynamic-gas oracle). Phase 5 (execution) is a **gated, default-OFF scaffold — not built**. Per-phase status is marked in the plan below.

The three structural edges to detect (math specified below):

1. **Complement arbitrage** — within a single binary market, `YES + NO ≠ 1`.
2. **NegRisk basket arbitrage** — within a multi-outcome event (N≥3 mutually exclusive outcomes), the sum of YES prices `≠ 1`. *This is where the majority of historical arb profit has lived.*
3. **Logical-dependency arbitrage** — across linked markets, a monotonicity/implication relationship is violated (e.g. `P(wins presidency) ≤ P(wins nomination)`).
4. **Cross-venue arbitrage** — *stub only* (Polymarket vs another venue). Scaffold the interface but do not implement; it carries resolution-source-equivalence risk and a jurisdiction problem. Leave a clear TODO and a warning.

---

## Non-negotiable constraints — read before writing any code

1. **Verify the live API first.** Before implementing any client, fetch and read the current docs. Do **not** trust endpoint paths, field names, auth flows, or rate limits from memory — including your own training data. Authoritative sources to read:
   - `https://docs.polymarket.com` (start at the API reference / introduction, then the Gamma, CLOB, Data, websocket, fees, and neg-risk pages)
   - `https://github.com/Polymarket/py-sdk` (the unified SDK — README, methods, install). NOTE: the old `py-clob-client` is archived and non-functional; its replacement is `polymarket-client` (pip, beta). See `docs/API_NOTES.md`.
   - Confirm the current settlement/collateral token (USDC vs the newer pUSD) and the split/merge + NegRisk-convert mechanics from the docs.
   Live-verified API facts (base URLs, key endpoints, auth, rate limits, identifier model) are captured — dated — in `docs/API_NOTES.md`. The rest of the build references that file, not assumptions.

2. **Read-only by default — hard rule.** The scanner must never create, sign, post, or cancel an order, and must never touch a private key, unless `EXECUTION_ENABLED=true` AND an interactive human confirmation is given at runtime. The default config and the default `scan` command are detection-only. Gamma and Data require no auth; CLOB **book reads** are public; only **trading** needs credentials. The detector must not even instantiate a signing client.

3. **No secrets in the repository, ever.** Ship `.env.example` with placeholders only. Real values come from the environment / a git-ignored `.env`. Load via `pydantic-settings`. A private key, if ever present, is read from env at execution time and never logged, printed, or written to disk.

4. **Respect rate limits and back off.** Read the live rate-limit table and implement a per-service token-bucket limiter (Gamma, CLOB, Data have different quotas; the websocket caps concurrent connections — confirm exact numbers from docs). Exponential backoff with jitter on HTTP 429 / Cloudflare throttling. Prefer the websocket for live book updates over polling; cache Gamma metadata (it changes infrequently).

5. **Resolution risk is a first-class filter, not an afterthought.** Markets resolve via the UMA oracle and can be disputed or manipulated. Every emitted opportunity carries a `resolution_risk` tag derived from its category/resolution source, and the default filter can hard-exclude at-risk markets. (Encode the lesson that "logically locked" still depends on a clean, verifiable resolution.)

6. **Phased build with review gates.** Work the phases below in order. At the end of **each** phase: run `ruff`, the type checker, and `pytest`; fix failures; commit with a Conventional Commits message; then **STOP** and post a short summary (what was built, what the tests prove, anything you had to decide) and wait for my go-ahead. Do not start the next phase unprivileged.

7. **Track progress with your todo tooling.** Maintain a visible checklist of phase tasks and check them off as you go.

8. **Tests don't hit the live API.** Unit/detector tests run against recorded fixtures (committed JSON). Provide a `scripts/record_fixtures.py` that captures live samples on demand. Only the explicit `scan --dry-run` end-to-end run touches the live (read-only) API.

---

## Tech stack (use these unless you hit a concrete blocker — then ask)

- **Python 3.12**, managed with **`uv`** (`pyproject.toml`, `uv.lock`).
- **`httpx`** (async) for Gamma/Data/CLOB REST; **`websockets`** for the CLOB feed.
- **`pydantic` v2** for typed domain models; **`pydantic-settings`** for config.
- **`polymarket-client`** (the `py-sdk` unified SDK; replaces the now-archived `py-clob-client`) — imported **only** inside the execution module, for order building/signing.
- **SQLite** (via stdlib `sqlite3`) to persist detected opportunities → enables backtest/replay and hit-rate analytics. (Make the storage layer an interface so Postgres is a drop-in later.)
- **`structlog`** for structured JSON logging.
- **`pytest`** + **`hypothesis`** (property-based tests for the invariant math).
- **`ruff`** (lint+format) and **`mypy`** (or pyright) — strict.
- **Docker** + **`docker-compose.yml`** — containerized so it drops into an existing Compose stack; long-running `scan` service + one-shot CLI.
- Optional **Prometheus `/metrics`** endpoint (opportunities seen, emitted, by detector; API latency; 429 count) for an existing monitoring stack.
- **GitHub Actions** CI: ruff + type-check + pytest on push/PR.
- Pluggable **notifier** (webhook / ntfy / Discord / Telegram) behind an interface; off unless configured.

---

## The math — implement and property-test exactly this

Let `a_yes`, `a_no` be best **ask** prices (what you pay to buy), `b_yes`, `b_no` best **bids**. All prices in [0,1]. `f(...)` = total taker fees for the legs (per-market, per-category; pull live params — some categories are fee-free). `g` = on-chain gas estimate per round trip.

**1. Complement (single binary market) — realizes instantly via merge.**
- **Under:** if `a_yes + a_no < 1`: buy 1 YES + 1 NO, **merge** the pair → receive 1 (USDC/pUSD), immediately, no resolution wait.
  `net_profit = 1 - (a_yes + a_no) - f - g`
- **Over:** if `b_yes + b_no > 1`: **split** 1 collateral → 1 YES + 1 NO (mint a set), sell both legs.
  `net_profit = (b_yes + b_no) - 1 - f - g`

**2. NegRisk basket (event with outcomes `o_1..o_N`, mutually exclusive & exhaustive) — realizes at resolution.**
- If `Σ_i a_yes,i < 1`: buy 1 YES of every outcome. Exactly one resolves to 1.
  `net_profit = 1 - Σ_i a_yes,i - f - g` (paid at resolution).
- **Capital efficiency:** hold the basket against `1` unit of collateral via the NegRisk mechanism. **WARNING — do not confuse the two:** the NegRisk *convert* function is a capital-efficiency tool (you pay 1, you get 1, **no profit**). The profit comes from buying the underpriced basket through the **standard order books**. Using convert to "capture" an underpriced sum is the wrong tool and locks collateral. Encode this distinction in code + a docstring + a test.
- Because it pays only at resolution, also compute:
  `annualized = (net_profit / cost) * (365 / days_to_resolution)`

**3. Logical dependency (markets A, B where A ⇒ B, so `P(A) ≤ P(B)` must hold).**
- Violation: `price(A) > price(B)`. Locked trade: **buy YES_B + buy NO_A**.
  - Cost `= a_yes,B + a_no,A = a_yes,B + (1 - a_yes,A)` (approx, ignoring spread).
  - Min payoff `= 1` (the worst case is A occurs → B occurs).
  - `net_profit ≥ price(A) - price(B) - f - g` (paid at resolution).
- The dependency graph (which markets imply which) is configured/declared, not inferred from text. Start with a small hand-curated set of relations (time-monotonic "by date X ≤ by date Y" pairs, nomination⊇presidency, championship⊆playoff-berth). Make adding a relation a one-liner. **Full design: `docs/RELATIONS.md`** — auto-generated total-order ladders vs hand-declared nesting DAGs, the market tag schema, prioritized seed relations, exclusions, and the resolution-fingerprint gate (mostly built in Phase 3).

**4. Cross-venue — stub.** Define the interface and a `resolution_equivalence_check()` that must pass before any cross-venue opp is emitted. Do not implement venue #2. Leave `NotImplementedError` + a TODO explaining the resolution-mismatch and jurisdiction caveats.

**Shared filters (the part that makes it non-naive):**
- **Executable size:** `size = min over legs of (cumulative book depth at or better than the arb price)`. Reject opportunities whose executable notional `< MIN_NOTIONAL`. Never report an opp that exists for one share.
- **Fee threshold:** only emit if `net_profit_bps ≥ MIN_PROFIT_BPS`. Threshold is lower for fee-free categories (e.g. geopolitics/world-events) and higher for fee'd categories — derive per market from live fee params.
- **Resolution-risk gate:** tag each opp; default-exclude `at_risk`.
- **Dedupe / cooldown:** don't re-alert the same opportunity every loop; key on (event, detector, price-bucket) with a cooldown window.

---

## Repository layout

```
polyarb/
  pyproject.toml
  uv.lock
  README.md
  SPEC.md                      # this file
  CLAUDE.md                    # session guide: rules, commands, architecture mental model
  .env.example                 # placeholders only — never real secrets
  .gitignore
  .pre-commit-config.yaml
  .github/workflows/ci.yml
  docker/
    Dockerfile
    docker-compose.yml
  docs/
    API_NOTES.md               # what you verified from live docs, dated
  scripts/
    record_fixtures.py         # capture live API samples for tests
  src/polyarb/
    __init__.py
    config.py                  # pydantic-settings
    models.py                  # Event, Market, Outcome, OrderBook, Opportunity
    logging_setup.py           # structlog configuration and JSON log setup
    recording.py               # live read-only sample capture → test fixtures
    clients/
      gamma.py                 # discovery: events/markets/negRisk flags (public)
      clob.py                  # public reads: order books, prices, midpoints, fees, tick/min-size
      data.py                  # positions/trades/holders (public)
      ws.py                    # live book updates (websocket)
      ratelimit.py             # per-service token buckets + backoff
    detectors/
      base.py                  # Detector protocol -> Iterable[Opportunity]
      complement.py
      negrisk_basket.py
      dependency.py
      crossvenue.py            # stub + resolution_equivalence_check()
    pricing/
      fees.py                  # per-market/category fee model -> net profit
      sizing.py                # executable size from book depth
    resolution/
      risk.py                  # classify resolution source -> risk tag
      relations.py             # declared logical-dependency graph
    engine/
      scanner.py               # fetch -> detect -> filter -> rank -> emit (async loop)
      filters.py               # fee/size/resolution/dedupe
      ranking.py               # sort by net_profit, annualized, risk
      backtest.py              # summarize stored opportunity history
      metrics.py               # optional Prometheus /metrics endpoint
    sinks/
      store.py                 # SQLite persistence (interface + impl)
      notify.py                # webhook/ntfy/discord (pluggable, optional)
    execution/                 # GATED — default OFF
      guard.py                 # EXECUTION_ENABLED check, max-notional cap, kill-switch, manual confirm  # Phase 5 — gated scaffold, default OFF
      executor.py              # multi-leg submission via polymarket-client (only behind guard)  # Phase 5 — gated scaffold, default OFF
    cli.py                     # `scan` (dry-run default), `backtest`, `replay`, `record`
  tests/
    fixtures/                  # recorded API JSON
    test_complement.py
    test_negrisk.py
    test_dependency.py
    test_fees.py
    test_sizing.py
    test_invariants.py         # hypothesis property tests for the math
    test_filters.py
```

---

## Phased plan with review gates

> After every phase: `ruff check` + `ruff format` + type-check + `pytest` must pass. Commit. Then STOP and summarize. Wait for my approval.

**Phase 0 — Bootstrap & verify.** ✅ *(complete)*
- Verify the live API (constraint #1) and write `docs/API_NOTES.md`.
- `git init`; scaffold the tree; `uv init` + `pyproject.toml` with deps; `.gitignore`; `.env.example`; `pre-commit` (ruff+mypy); `.github/workflows/ci.yml`; a `CLAUDE.md` capturing the constraints and stack so future sessions stay consistent; a stub `README.md`.
- **DoD:** repo builds, `uv run python -c "import polyarb"` works, CI is green on an empty test, `API_NOTES.md` exists and is dated. **Gate.**

**Phase 1 — Clients & models.** ✅ *(complete)*
- Implement `gamma`, `clob` (public reads only), `data`, `ws`, `ratelimit`. Pydantic models for Event/Market/Outcome/OrderBook. Resolve the condition_id ↔ token_id identifier model correctly.
- `scripts/record_fixtures.py` captures real samples; commit a representative fixture set (include at least one binary market, one multi-outcome NegRisk event, and one fee-free + one fee'd category).
- Offline tests parse fixtures into models.
- **DoD:** `uv run polyarb record` pulls live read-only data; client tests pass offline. **Gate.**

**Phase 2 — Detectors, pricing, property tests.** ✅ *(complete)*
- Implement `complement`, `negrisk_basket`, `dependency`; `pricing/fees.py`, `pricing/sizing.py`; `resolution/relations.py` seed graph.
- Property-based tests (`hypothesis`) prove: complement/basket/dependency profit formulas are correct across random price vectors; "no false positive" (never flags a non-arb); NegRisk-convert-is-not-arb invariant; fee threshold monotonicity.
- **DoD:** all detector + math tests pass, including property tests. **Gate.**

**Phase 3 — Engine, filters, sinks (the working dry-run scanner).** ✅ *(complete)*
- `engine/scanner.py` async loop: discover candidate events via Gamma → stream/read books via CLOB/ws → run detectors → apply `filters` (fee/size/resolution/dedupe) → `ranking` → `sinks/store` (SQLite) + structured log + optional notifier.
- `cli.py scan --dry-run` (the default) produces a live, ranked, net-of-fees opportunity feed and writes to SQLite. No order code runs.
- **DoD:** a real `scan --dry-run` against the live read-only API runs for several minutes, logs ranked opportunities (or a clean "none over threshold"), and persists them. **Gate.**

**Phase 4 — Hardening, container, analytics.** ✅ *(complete)*
- `Dockerfile` + `docker-compose.yml` (a long-running `scan` service); optional Prometheus `/metrics`; graceful shutdown; structured-log polish.
- `cli.py backtest`/`replay` over stored opportunities (hit rate, would-be P&L, time-to-resolution distribution, by detector/category). `README.md` with run + deploy instructions.
- **DoD:** `docker compose up` runs the scanner; backtest command produces a summary over the SQLite history. **Gate.**

**Phase 5 — Execution module (SCAFFOLD ONLY, default OFF). Do not enable.** ⬜ *(not built — guard.py/executor.py not yet created)*
- `execution/guard.py`: refuses unless `EXECUTION_ENABLED=true`, enforces a max-notional-per-trade cap and a global kill-switch, and requires an interactive `yes` confirmation per trade. `execution/executor.py`: multi-leg submission via `polymarket-client` (the unified `py-sdk`; `py-clob-client` is archived), callable **only** through the guard, with FOK/IOC order types to minimize leg risk, and a `--paper` mode note (since there's no testnet, real paper = tiny live orders).
- Loud warnings in code + README. The default `scan` path must remain execution-free.
- **DoD:** with `EXECUTION_ENABLED` unset, the executor cannot run and tests assert that. Leave it disabled. **Gate — then we stop and decide together whether to ever turn it on.**
