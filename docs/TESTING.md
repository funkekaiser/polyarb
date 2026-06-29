# Testing & Bug-Hunt Guide

A guide to how `polyarb`'s correctness is defined and verified, what an adversarial bug
hunt found (and how each was fixed), the limitations we *chose* to leave, and how to dig in
further yourself. Written 2026-06-29 after a two-agent bug hunt.

---

## 1. What "correct" means here

`polyarb` is a **detector**, so correctness is mostly about *not lying*: never reporting an
opportunity that isn't real, and pricing the real ones net-of-fees. The load-bearing
invariants (the things tests defend) are:

- **Profit identities (SPEC §"The math").** For each detector the gross-profit formula is
  exact, and the detector emits **only when `net_profit > 0`** — i.e. emit ⇒ the underlying
  identity is genuinely violated. "No false positive" is the headline property.
  - Complement: `gross = 1 − (a_yes + a_no)` (under) / `(b_yes + b_no) − 1` (over).
  - NegRisk basket: `gross = 1 − Σ a_yes,i`.
  - Dependency (`A⇒B`): buy `YES_B + NO_A`, `gross = 1 − (a_yes,B + a_no,A) ≈ price(A) − price(B)`.
- **NegRisk convert is not arbitrage** — `negrisk_convert_pnl == 0` for any prices.
- **Fees never increase profit** — `taker_fee ≥ 0`, parabolic, zero at the price extremes.
- **Executable-size floor** — an opp's size is the thinnest leg's book depth; below
  `MIN_NOTIONAL` it's dropped (never report a one-share arb).
- **Relation sign convention (RELATIONS.md §1)** — every generated relation `A⇒B` encodes
  `price(A) ≤ price(B)` with `A` the stronger/cheaper leg. Ladders are numeric/chronological,
  adjacent-rung only; the §6 fingerprint gate forbids pairing markets that settle differently.
- **Read-only** — no signing client is ever constructed on the scan path.

These live as property tests (`hypothesis`) in `tests/test_invariants.py`,
`tests/test_fees.py`, and `tests/test_relations.py`.

## 2. The test suite

Everything runs **offline** against recorded fixtures (`tests/fixtures/`) or `MockTransport`;
no test hits the live API.

| File | Covers |
|---|---|
| `test_invariants.py` | property tests for the profit identities + no-false-positive + convert-is-not-arb |
| `test_fees.py` / `test_sizing.py` | fee formula (monotonic, symmetric, bounded) / book-depth sizing |
| `test_complement.py` / `test_negrisk.py` / `test_dependency.py` | per-detector emit / no-emit / edge cases |
| `test_relations.py` | ladder directions, equal-bound handling, fingerprint cohorts, DAG closure & exclusions |
| `test_models.py` / `test_clients.py` | API-quirk parsing (JSON-string fields, blank→None, end dates) / clients via MockTransport |
| `test_filters.py` / `test_ranking.py` / `test_risk.py` | thresholds + dedupe / ordering / risk tagging |
| `test_store.py` / `test_notify.py` / `test_metrics.py` | SQLite round-trip / webhook+null notifier / Prometheus counters |
| `test_scanner.py` | end-to-end discover→detect→filter→rank→persist over MockTransport |
| `test_backtest.py` | analytics summary (counts, bps stats, P&L, median) |
| `test_bugfixes.py` | **regression tests for every bug in §4** |

### Running

```bash
uv sync --dev                      # REQUIRED first (auto-sync is off; see CLAUDE.md)
uv run pytest                      # full suite
uv run pytest tests/test_bugfixes.py -v
uv run pytest -k naive_end_date    # one test by name expression
uv run ruff check . && uv run mypy src
```

To push the property tests harder, raise the hypothesis example count:

```bash
uv run pytest tests/test_invariants.py --hypothesis-seed=random -p no:cacheprovider
# or set a profile in code via hypothesis.settings(max_examples=1000)
```

## 3. How the bug hunt was run (reproducible)

Two `general-purpose` subagents (Sonnet), each in its **own git worktree** (isolated
checkout + venv, so they could run `uv`/`pytest` in parallel without the shared-venv race —
see `[[venv-no-sync-fix]]`). One probed the **math/detectors/relations/ranking**, the other
the **models/engine/sinks**. Each wrote adversarial + property tests, *ran* them, and
reported concrete failing cases classified as CONFIRMED BUG / SPEC-DEVIATION /
MODELING-LIMITATION / NON-ISSUE. Findings were then triaged by hand (not all subagent
suggestions were accepted — see §5/§6), fixed in `main`, and pinned with regression tests.

To repeat: launch a subagent with `isolation: "worktree"`, point it at a subsystem, tell it
to hunt boundaries/invariants and *classify + minimally reproduce* each finding, then triage
yourself. Clean up with `git worktree remove --force <path>` afterward.

## 4. Bugs found and fixed

All fourteen are covered by `tests/test_bugfixes.py` (first hunt: 1–8; second hunt: 9–14).

| # | Sev | Where | Bug | Fix |
|---|-----|-------|-----|-----|
| 1 | High | `engine/scanner.py` `_days_to_resolution` | A timezone-**naive** `endDate` (the ISO `Z` is optional) made `aware_now − naive` raise `TypeError`, which the loop caught as `scan_pass_failed` — **one bad market dropped the entire pass**. | Treat naive end dates as UTC before subtracting. |
| 2 | Med | `pricing/fees.py` `taker_fee` | For `price ∉ [0,1]` the parabola `p·(1−p)` goes **negative**, and a negative fee *inflates* `net_profit`. | Guard: return 0 outside `[0,1]`. |
| 3 | Med | `detectors/base.py` `make_opportunity` | `days_to_resolution == 0` (resolves **today**) is falsy → `annualized` stayed `None` → ranked **last** (and a latent divide-by-zero). | `days_to_resolution is not None`, floor days at 1. |
| 4 | Low | `models.py` `OrderBook` | A timestamp sent as a fractional JSON float failed `int` validation. | Coerce `float` (and `str`) → `int`. |
| 5 | Med | `models.py` `yes_outcome` / `Event.outcomes` | Non-binary markets (legal — untradeable markets omit `clobTokenIds`) caused `IndexError`. | `yes_outcome` raises a clear `ValueError`; `Event.outcomes` skips non-binary. |
| 6 | Low | `sinks/notify.py` | `WebhookNotifier`'s self-created `AsyncClient` was never closed (no `aclose` on the `Notifier` protocol). | Add `aclose` to the protocol + `NullNotifier`; CLI closes the notifier on shutdown. |
| 7 | Med | `detectors/dependency.py` + `resolution/relations.py` | A self-loop relation `A⇒A` made the dependency detector buy `YES_A + NO_A` (a *complement*) and mislabel it "dependency violation". | Detector skips `antecedent == consequent`; `add_relation` rejects self-loops. |
| 8 | Med | `resolution/relations.py` ladder | Two markets with the **same** bound sorted adjacent and got a spurious relation (equal bounds are unordered — no implication). | Skip equal-bound adjacent pairs in all three ladders. |

### Second hunt (2026-06-29) — six more, all in `tests/test_bugfixes.py` (Bugs 9–14)

A second three-agent hunt (detectors/pricing, engine/sinks, clients/models) plus an Opus
"statistician committee" on strategy soundness. The mechanical bugs below were fixed; the
committee's *strategy* critiques (sizing-walks-only-top-of-book, gas defaulted to 0, no
staleness/adverse-selection gate, the dead `AT_RISK` tag as a risk-policy choice, ranking
objective) are tracked separately as design decisions, not yet implemented.

| # | Sev | Where | Bug | Fix |
|---|-----|-------|-----|-----|
| 9 | Med | `detectors/dependency.py` | `days = get(B) or get(A)` — a legitimate `days_to_resolution == 0` (resolves today) is falsy, so B's horizon was silently replaced by A's, mis-annualizing the opp by up to 365×. | Explicit `None` check instead of `or`. |
| 10 | Med | `engine/scanner.py` `_fetch_books` | `one()` caught only `httpx.HTTPError`; a malformed CLOB payload (bad JSON / validation error) propagated out of `asyncio.gather` and **killed the whole pass** — same class as Bug 1, adjacent code. | Also catch `Exception` (not `BaseException`/cancel), log, skip the token. |
| 11 | Med | `sinks/notify.py` `WebhookNotifier` | `httpx.InvalidURL` (a malformed `NOTIFIER_URL`) is **not** an `httpx.HTTPError`, so it escaped `notify()` and wedged the emit loop, suppressing every subsequent opp for a cooldown window. | Broaden the second `except` to `Exception`; the protocol guarantees `notify()` never raises. |
| 12 | Low | `models.py` `OrderBook._coerce_timestamp` | A fractional timestamp delivered as a **string** (`"…​.9"`) raised on `int("…​.9")` (Bug 4 only covered the float case). | Coerce via `int(float(value))`. |
| 13 | Low | `models.py` `best_bid`/`best_ask` | A phantom **zero-size** level at a better price became the "best" quote → opp with `executable_size 0`, masking the real fillable level behind it. | Skip `size == 0` levels when computing the best quote. |
| 14 | Low | `resolution/risk.py` `classify_market` | Substring test `"politics" in fee_type` also matches **"geopolitics"** — a future paid-geopolitics tier would be mis-tagged `ELEVATED`. | Exclude `"geopolitics"` from the politics branch. |

Two flagged-but-not-fixed items from this hunt: the rate-limiter holding its lock across
`asyncio.sleep` (assessed — the per-bucket serialization is *correct* rate-limiting, not a
bug) and the websocket async-generator leaking a connection if abandoned mid-stream
(forward-looking: the scan loop uses REST polling today, so `ws.py` is off the live path).
The emit loop also now guards each opp's store/notify independently, and the scanner's stop
log reports `attempts` (not a count that contradicted `totals["passes"]`).

## 5. Known limitations we deliberately did **not** "fix"

These are real, but they're either correct-by-design or better handled elsewhere. Listed so
you can revisit deliberately.

- **Executable size ignores cross-leg price impact.** `size = min(per-leg depth at that
  leg's best price)`. Filling the thin leg can push other legs to worse prices, so the figure
  is an *upper bound*. This matches SPEC's definition; `MIN_NOTIONAL` partially compensates.
  To improve: walk the books jointly and compute the size at which the *combined* VWAP still
  clears the threshold. (`pricing/sizing.py`.)
- **NegRisk basket can emit a sub-cent edge from Decimal artifacts** (e.g. three `1/3`s sum
  to `0.999…9`). Real book prices are tick-quantized so this needs pathological inputs, and
  the engine's `MIN_PROFIT_BPS` filter drops it. If you ever call detectors without the
  engine filter, add a small epsilon floor.
- **Dependency "violation" label is imprecise** when the edge actually comes from a cheap
  `NO_A` spread rather than a crossed price ordering. The trade is still sound (payoff ≥ 1).
- **DAG generator: duplicate node id within one underlying** silently keeps the last market.
  Tags are hand-curated, so this is a curation error; consider a validation pass over
  `TAG_REGISTRY` if you automate tagging.
- **SQLite indexed columns store `float(...)`** (lossy). The JSON `payload` is the canonical,
  exact record and `recent()` reconstructs from it; only ad-hoc SQL on the numeric columns
  sees float imprecision.

## 6. One open design decision for you — ranking order

`engine/ranking.py` sorts by **`(resolution_risk, −net_profit_bps, −annualized)`** — i.e.
*safest resolution first*, then profit, then annualized. SPEC's repo-layout note says
"sort by net_profit, annualized, risk". These genuinely differ: today's code will rank a
safe-but-smaller edge above a richer riskier one.

I left the current (risk-first) behavior because resolution risk is described as a
"first-class filter" and ranking safety-first is defensible — but it's your call. If you'd
rather follow SPEC's literal priority (annualized as a primary signal, risk last), say so and
I'll flip `_sort_key` and update the tests.

## 7. Where bugs are most likely to hide (dig here next)

1. **Live-API quirk absorption** (`models.py`). Every real bug so far came from a payload
   shape the fixtures didn't have (blank strings, naive dates, float timestamps,
   non-binary markets). New Gamma/CLOB fields or formats are the highest-yield hunting ground
   — run `polyarb record` to refresh fixtures and diff.
2. **Relation sign conventions** (`resolution/relations.py`). Direction errors are silent and
   load-bearing; `test_relations.py` pins them, but any new comparator needs the same
   `price(A) ≤ price(B)` proof.
3. **Decimal vs float seams** — fee math, sizing, the SQLite columns.
4. **Timezone handling** — anything subtracting datetimes; prefer aware-UTC end to end.
5. **The scan loop's resilience** — a single bad market shouldn't drop a pass (bug #1 was
   exactly this); prefer per-item `try`/skip over per-pass failure.

A fast way to extend the hunt: add a `hypothesis` property to `test_invariants.py` asserting
a new invariant, crank `max_examples`, and let it search for a counterexample.
