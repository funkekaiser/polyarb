# polyarb — guided demo & codebase tour

A 15-minute walkthrough to understand what this project does, see it run, and know where
every piece lives. Read top-to-bottom or jump to a section.

> **One-time setup** (per the venv rules in CLAUDE.md): `uv sync --dev`.
>
> Every command below is prefixed with `PYTHONPATH=src`. On macOS, `uv run` can re-apply the
> hidden flag to the editable-install `.pth` file; Python 3.12 then silently skips it and
> `import polyarb` fails with `ModuleNotFoundError`. `PYTHONPATH=src` puts the source tree on
> `sys.path` directly, so the commands work regardless of that flag — now and in the future.
> (One-shot rescue if you hit it anyway: `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`.
> Full story in CLAUDE.md → "venv".)
>
> **For a long-running / production scanner, use Docker instead** — a Linux image where this
> macOS issue does not exist:
> `docker compose -f docker/docker-compose.yml up --build`
> (one-shot form: `docker compose -f docker/docker-compose.yml run --rm scan version`).

---

## 1. The one-sentence pitch

polyarb watches Polymarket for prices that **violate a mathematical identity** — not prices
we think are *wrong* (that would be forecasting), but prices that are *inconsistent with each
other* and therefore lock in a risk-free profit no matter how the world resolves. It detects,
scores **net of fees**, ranks, persists, and (optionally) alerts. **Detection is the product.**
Placing orders is a separate, default-OFF, not-yet-built module (Phase 5).

The cardinal rule: **read-only by default.** The scan path never signs, posts, or cancels an
order and never touches a private key. (Gamma/Data need no auth; CLOB book reads are public;
only *trading* needs credentials — and trading isn't built.)

---

## 2. The three strategies (the math you're detecting)

| Strategy | Identity it exploits | The trade | Realizes |
|---|---|---|---|
| **Complement** | Within one binary market, `YES_ask + NO_ask < 1` | Buy 1 YES + 1 NO for < $1; exactly one pays $1 | **Instant** |
| **NegRisk basket** | Across N≥3 mutually-exclusive outcomes, `Σ YES_ask < 1` | Buy one YES of each; exactly one pays $1 | At **resolution** |
| **Dependency** | A declared implication `A ⇒ B` is violated: `NO_A_ask + YES_B_ask < 1` | Buy NO_A + YES_B; covers every joint outcome | At **resolution** |

A fourth, **cross-venue**, is a deliberate stub (`NotImplementedError`) — it carries
resolution-source-equivalence and jurisdiction risk, so it's scaffolded but not implemented.

The exact profit identities (cost, gross, net-of-fees, payoff proofs) are in **SPEC.md →
"The math"** and are property-tested with `hypothesis`.

---

## 3. See it run — offline, deterministic (start here)

This needs no network and no credentials. It builds three synthetic-but-realistic books —
one per detector, each containing a genuine arb — and runs them through the **real** pipeline
(detect → tag resolution risk → filter → rank), printing the per-set profit breakdown and the
filter's drop accounting:

```bash
PYTHONPATH=src UV_NO_SYNC=1 uv run python scripts/demo.py
```

What to watch for as it prints:
- each detector finding its opportunity and the **net-of-fees** per-set profit (never gross);
- `executable_size` coming from **book depth** (never a one-share opp);
- the **filter** dropping anything below `min_profit_bps` / `min_notional` / at-risk resolution;
- the **ranking**: safest resolution first, then highest bps, then best annualized.

The math you see here is exactly the math the live scanner applies — only the data source
differs (hand-built books instead of the live CLOB).

---

## 4. See it run — live, read-only

These hit the live Polymarket API (read-only; still no signing client):

```bash
PYTHONPATH=src UV_NO_SYNC=1 uv run polyarb version                 # smoke check
PYTHONPATH=src UV_NO_SYNC=1 uv run polyarb scan --dry-run --passes 1   # one real discover→detect→rank pass
PYTHONPATH=src UV_NO_SYNC=1 uv run polyarb backtest                # summarize stored opportunity history
PYTHONPATH=src UV_NO_SYNC=1 uv run polyarb replay                  # re-print the persisted feed, oldest-first
```

`scan --dry-run` is the default path. `--no-dry-run` deliberately raises — execution is Phase 5
and gated. `record` captures live read-only samples into fixtures.

---

## 5. The pipeline, end to end

```
discover ──▶ read books ──▶ detect ──▶ filter ──▶ rank ──▶ emit
 Gamma        CLOB (public   3 detectors  profit/    risk-adj   store + log
 events/      book reads)    + risk tag   notional/  annualized  + notify
 markets                                  risk/dedupe
```

Driven by `engine/scanner.py::scan_once`. One pass: discover candidate markets (Gamma),
fetch their books concurrently (CLOB), run the three detectors, tag each opportunity's
resolution risk, filter, rank, then persist + log + optionally notify.

---

## 6. Where everything lives (the file map)

```
src/polyarb/
  cli.py            Typer entry: version / scan / record / backtest / replay
  config.py         pydantic-settings; thresholds, paths, feature flags
  models.py         typed domain: Event, Market, Outcome, OrderBook, Opportunity
  clients/          Polymarket access — READ ONLY
    gamma.py        events/markets discovery
    clob.py         public book/price/midpoint reads
    data.py         trades/positions
    ws.py           market-channel websocket
    ratelimit.py    per-service token bucket + jittered backoff
    base.py         shared async HTTP
  detectors/        each → Iterable[Opportunity] (base.py Detector protocol)
    complement.py   negrisk_basket.py   dependency.py   crossvenue.py (stub)
  pricing/
    fees.py         net-of-fees from live fee params
    sizing.py       executable size from cumulative book depth (reject < MIN_NOTIONAL)
  resolution/
    risk.py         resolution-source → risk tag (default-exclude AT_RISK)
    relations.py    hand-declared dependency graph (ladders + DAGs); never inferred
  engine/
    scanner.py      the discover→…→emit loop
    filters.py      profit / notional / risk / dedupe-cooldown
    ranking.py      sort: risk bucket, then bps, then annualized
    backtest.py     summarize stored history
    metrics.py      optional Prometheus /metrics
  sinks/
    store.py        SQLite behind an interface (Postgres is a drop-in later)
    notify.py       optional webhook/ntfy/Discord/Telegram
  execution/        GATED, default OFF, scaffold only — Phase 5, not built
```

---

## 7. Correctness — why you can trust the numbers

```bash
PYTHONPATH=src UV_NO_SYNC=1 uv run pytest        # fully offline (committed JSON fixtures)
PYTHONPATH=src UV_NO_SYNC=1 uv run ruff check . && PYTHONPATH=src uv run mypy src
```

- The **profit math is property-tested** (`hypothesis`) against the SPEC identities.
- Tests **never hit the live API** — they run on recorded fixtures in `tests/fixtures/`.
- `docs/TESTING.md` records how correctness is defined, the adversarial bug-hunt findings +
  fixes, and where bugs are most likely to hide.

---

## 8. Doc map (one home per fact)

- **SPEC.md** — design source of truth: the math, constraints, stack, layout, phase plan.
- **CLAUDE.md** — session rules, commands, the architecture mental model, the venv gotchas.
- **docs/API_NOTES.md** — live-verified API facts (base URLs, quotas, fees, payload quirks).
- **docs/RELATIONS.md** — the dependency-subsystem spec (ladders, DAGs, risk tiers).
- **docs/TESTING.md** — correctness definition, test map, bug-hunt log, known limits.

---

## 9. Status & what's next

Phases 0–4 (+3b) are complete: read-only clients + models, the three property-tested
detectors, the relations subsystem, the full `scan --dry-run` pipeline, and
hardening/analytics (Docker, backtest/replay, graceful shutdown, structured logs, optional
Prometheus). Remaining: **Phase 5** — the execution module, which stays default-OFF and is a
review-gated stop. Don't start it without an explicit go-ahead.
