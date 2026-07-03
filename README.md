# polyarb

> A read-only **Polymarket structural-arbitrage scanner** — it finds prices that violate a
> mathematical identity, not a forecasting opinion.

[![CI](https://github.com/funkekaiser/Polymarket-Arbitrage/actions/workflows/ci.yml/badge.svg)](https://github.com/funkekaiser/Polymarket-Arbitrage/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![mode: read-only](https://img.shields.io/badge/mode-read--only-informational)

polyarb continuously scans Polymarket for opportunities where prices break an identity that
*must* hold — e.g. a binary market's `YES + NO` should equal `$1`. It scores each candidate
**net-of-fees**, ranks by risk-adjusted / annualized return, persists it, and can alert. Books are
read **WebSocket-first** (an in-memory cache off the CLOB market channel, REST-confirmed before
emit; REST polling is the resync/backup). **Detection is the product** — order execution is a
separate, opt-in, default-OFF module that is deliberately not built.

## The honest result 📉

The most interesting thing in this repo isn't that it finds arbitrage — it's what happened when I
took the output seriously. I pointed the scanner at the whole ~600-market board, left it running,
and used its own tooling plus a **three-lens statistician committee** to answer a simple question:
*is any of this worth trading?*

**53 hours of continuous live scanning (July 1–3, 2026), in one table:**

| | |
|---|---|
| Full-board scan passes | **29,778** — ~825 order books, one WebSocket-fed sweep every ~6 s |
| Reliability | 1 container, **0 restarts** · 86 WS drops, **every one auto-recovered** · healthcheck green throughout |
| Structural mispricings found | **exactly 1** — a 7-leg NegRisk basket priced at Σ YES = $0.977 ("OpenAI IPO market cap") |
| What it was worth | **+75.8 bps net of fees** — but only **$9.77** deployable, locked **183 days** → **~1.5%/yr**, ~**$0.07** total |
| How long it lived | visible for the first **~6 h** of the run, then repriced — never seen again in the following **~47 h** |
| Opportunities clearing every gate | **0** |

<p align="center">
  <img src="reports/annualized_vs_benchmarks.png" width="560" alt="Annualized return of the one live arb vs. savings-account benchmarks">
</p>

The committee was unanimous: **do not chase it.** The one edge on the board returned *less than a
high-yield savings account* — before its void risk and six-month capital lock — and was probably
not even fillable at quoted size (the microstructure lens predicted stale liquidity; its
disappearance hours later fits). The full write-up — data, charts, the three independent verdicts,
and the endurance-run addendum — is **[reports/floor-analysis.md](reports/floor-analysis.md)**.

That's the point of the project: a rigorous detector, and the intellectual honesty to prove, with
its own instrumentation, that today's board is essentially arb-free at retail size. **The
engineering is the deliverable** — and "your market is efficient" is a finding, not a failure.

## What it detects

1. **Complement** — within a binary market, `YES + NO ≠ 1` (realizes instantly via merge/split).
2. **NegRisk basket** — within an N≥3 mutually-exclusive event, `Σ YES ≠ 1` (realizes at resolution).
3. **Logical dependency** — across linked markets where `A ⇒ B`, the identity `P(A) ≤ P(B)` is violated.
4. **Cross-venue** — *deliberate stub* (resolution-equivalence + jurisdiction caveats make it unsafe).

Every candidate clears net-of-fees profit, an executable-size floor (per-leg order minimums), an
annualized-return gate, resolution-risk gating, and de-duplication before it's ever emitted.

## Try it in 10 seconds (offline, no network)

```bash
uv sync --dev
uv run python scripts/demo.py
```

`scripts/demo.py` builds three synthetic-but-realistic scenarios (one per detector), each with a
genuine arb, and runs them through the **real** detect → tag-risk → filter → rank pipeline — the same
code the live scanner uses, only the data source differs. No API keys, no Docker, no network.

## Run it for real (Docker)

```bash
make docker-up        # build + start the long-running read-only scanner (background)
make docker-logs      # tail the structured JSON logs
make docker-down      # stop (SQLite history is preserved in a named volume)
```

Query the stored history (each in a throwaway container):

```bash
docker compose -f docker/docker-compose.yml run --rm scan backtest   # summary + realized P&L
docker compose -f docker/docker-compose.yml run --rm scan ledger      # distinct opps, one line each
```

**→ Full operating guide: [docs/POLYARB_DOCS.md](docs/POLYARB_DOCS.md)** — how the container works,
every config knob, where results go, the realized-outcome ledger, and a manual/local run.

## How it works

```
discover events (Gamma) → read books WebSocket-first (CLOB) → detect identity violations
     → filter (fees · executable size · annualized · resolution risk · dedupe) → rank ($, then risk)
     → emit (SQLite ledger + optional alert)  ──▶  settle: poll resolutions → realized P&L (E1/E2)
```

The scanner maintains an in-memory book cache off the market-channel websocket (hardened with a
stall watchdog, dynamic resubscribe, and a freshness guard); a candidate found off the cache is
REST-confirmed before emit. Emitted opportunities are deduped into an **economic-event ledger**; a
read-only `settle` poller then records how each one actually resolved and alarms if a "guaranteed"
lock ever settles negative.

## Design & docs

| Doc | What it is |
|---|---|
| [SPEC.md](SPEC.md) | Design source of truth — the profit math, the constraints, the phased plan. |
| [docs/POLYARB_DOCS.md](docs/POLYARB_DOCS.md) | Operating guide — Docker, config, results, the E1/E2 ledger. |
| [reports/floor-analysis.md](reports/floor-analysis.md) | The honest-result study — live data, statistician-committee verdict, 53-hour endurance addendum. |
| [docs/API_NOTES.md](docs/API_NOTES.md) | Live-verified Polymarket API facts (endpoints, fees, gas, quirks), dated. |
| [docs/TESTING.md](docs/TESTING.md) | How correctness is earned — the test map + adversarial bug-hunt findings. |
| [docs/STRATEGY_BACKLOG.md](docs/STRATEGY_BACKLOG.md) | The strategy/decision log from the committee reviews. |
| [CLAUDE.md](CLAUDE.md) | The AI-assisted engineering process and rules used to build this. |

Stack: Python 3.12, `asyncio` + `httpx` + `websockets`, `pydantic` domain models, SQLite,
`structlog`, Typer CLI — `uv`-managed, ~465 offline tests, strict `mypy`, `ruff`.

## Hard rules

- **Read-only by default.** No order is signed, posted, or cancelled and no private key is touched
  unless `EXECUTION_ENABLED=true` **and** a human confirms at runtime. The default scan path never
  even instantiates a signing client.
- **No secrets in the repo** — `.env.example` holds placeholders only; real values load from env.
- **Verify the API against live docs**, never from memory — `docs/API_NOTES.md` is kept current and dated.

## Disclaimer

Engineering guidance only, **not financial advice**. Whether to ever enable execution — and the ToS,
jurisdiction, and tax questions that come with trading — is a separate decision to make with
appropriate professional advice.

## License

[MIT](LICENSE) © Jonathan Funke-Kaiser
