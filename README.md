# polyarb

Read-only **Polymarket structural-arbitrage scanner**. It continuously scans for
opportunities where prices violate a mathematical identity (not a forecasting opinion),
scores them net-of-fees, ranks by risk-adjusted/annualized return, persists, and alerts.

**Detection is the product.** Execution is a separate, opt-in, default-OFF module.

> Status: Phase 0 (bootstrap) complete. See `SPEC.md` for the full design, the math, and
> the phased plan; `docs/API_NOTES.md` for verified live-API facts; `CLAUDE.md` for the
> working rules.

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

## Planned commands (later phases)

```bash
uv run polyarb scan --dry-run   # default, read-only ranked opportunity feed
uv run polyarb record           # capture live samples → test fixtures
uv run polyarb backtest         # analyze stored opportunities
```

## Disclaimer

Engineering guidance only, not financial advice. Whether to ever enable execution — and
the ToS, jurisdiction, and tax questions that come with trading — is a separate decision
to be made with appropriate professional advice.
