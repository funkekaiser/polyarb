# polyarb

Read-only **Polymarket structural-arbitrage scanner**. It continuously scans for
opportunities where prices violate a mathematical identity (not a forecasting opinion),
scores them net-of-fees, ranks by risk-adjusted/annualized return, persists, and alerts.

**Detection is the product.** Execution is a separate, opt-in, default-OFF module.

It reads books **WebSocket-first**: an in-memory cache is kept fresh from the CLOB market channel,
a candidate detected off the cache is REST-confirmed before it's emitted, and the REST poll is the
resync/backup. (Set `STREAMING_ENABLED=false` to fall back to pure REST polling.)

> Status: Phases 0–4 complete; WebSocket streaming is the default read path (verified live in
> Docker); Phase 5 (execution) gated off.

## Structural edges detected

1. **Complement** — within a binary market, `YES + NO ≠ 1` (realizes instantly via merge/split).
2. **NegRisk basket** — within an N≥3 mutually-exclusive event, `Σ YES ≠ 1` (realizes at resolution).
3. **Logical dependency** — across linked markets, `A ⇒ B` so `P(A) ≤ P(B)` is violated.
4. **Cross-venue** — *stub only* (resolution-equivalence + jurisdiction caveats).

## Run it (Docker — the recommended way)

```bash
make docker-up        # build + start the long-running scanner in the background
make docker-logs      # tail the structured JSON logs
make docker-down      # stop (SQLite history is preserved in a named volume)
```

The container runs `scan --dry-run` (continuous, read-only), restarts unless stopped, and
persists everything it finds to a volume that survives restarts and rebuilds. Results live in
SQLite at `/data/polyarb.db`; query them with `… run --rm scan backtest` / `replay`.

**→ Full operating guide: [docs/POLYARB_DOCS.md](docs/POLYARB_DOCS.md)** — how the container
works, configuration, where results go, and the manual/local run (kept as a backup).

## Hard rules

- **Read-only by default.** No order is signed/posted/cancelled and no private key is touched
  unless `EXECUTION_ENABLED=true` **and** a human confirms at runtime.
- **No secrets in the repo** — `.env.example` holds placeholders only; load real values via env.
- **Verify the API against live docs**, never from memory. Keep `docs/API_NOTES.md` current.

## Learn more

- **`SPEC.md`** — the design source of truth: the math, the constraints, the repo layout, the phased plan.
- **`docs/POLYARB_DOCS.md`** — operating guide (Docker-first; config; results; troubleshooting).
- **`docs/API_NOTES.md`** — verified live-API facts (endpoints, fees, gas), dated.
- **`CLAUDE.md`** — working rules + the full doc map.

## Disclaimer

Engineering guidance only, not financial advice. Whether to ever enable execution — and the
ToS, jurisdiction, and tax questions that come with trading — is a separate decision to be
made with appropriate professional advice.
