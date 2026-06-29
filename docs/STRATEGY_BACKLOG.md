# Strategy backlog — committee review (2026-06-29)

Findings from a three-seat Opus "statistician committee" (identity soundness, execution
realism, risk/inference) on the *strategies and modeling* — distinct from the line-level
bug-hunt (those are fixed; see `docs/TESTING.md §4`). Nothing here is implemented yet; this
is the worklist. **Principle: perfect one thing at a time.**

Severity is the committee's; "strategy" is which detector(s) it touches
(C = complement, B = NegRisk basket, D = dependency, ✶ = cross-cutting).

## Tier A — "Guaranteed $1" can actually be $0 (is the math really risk-free?)

| # | Str | Sev | Issue | Fix direction | Status |
|---|-----|-----|-------|---------------|--------|
| A1 | B | HIGH | Basket never verifies **collective exhaustiveness** — only `neg_risk && len≥3` (mutual exclusivity). `negRiskAugmented` events are non-exhaustive by construction (the flag is ignored). If no listed outcome wins → basket pays **$0**. | Exclude augmented events; require a declared "exhaustive partition" tag (declared, not inferred). | open |
| A2 | B, D | HIGH | **Void / 50-50 resolution** breaks the floor. Legs are *different* markets that can void independently; an asymmetric void (the winning leg → $0.50, losers → $0) pays $0.50 on a $0.90 cost. Complement is immune (same-market two sides → 0.5+0.5=1). | State the {0,1}-resolution assumption; per-market void-prone flag and/or a payoff haircut for held arbs. | open |
| A3 | B, D | HIGH | **No staleness / cross-leg time-skew gate.** Books are independent REST reads at different times (basket's missing legs fetched in a *second* round); `timestamp_ms` never checked. A many-leg "Σ<1" can be a pure time-skew artifact. | Reject opps whose legs' timestamps skew beyond a budget; prefer the WS feed for synchronous books. | open |

## Tier B — Silently dropping / missing real money

| # | Str | Sev | Issue | Fix direction | Status |
|---|-----|-----|-------|---------------|--------|
| B1 | B,C,D | HIGH | **Sizing never walks past top-of-book** — each detector passes the *best* price as the depth limit, so `executable_size` = thinnest leg's *top level only*. Systematically under-sizes and drops real basket arbs. (`TESTING.md §5`'s "upper bound" note is backwards — it's a conservative lower bound.) | Walk legs jointly; accumulate size while combined VWAP still clears `MIN_PROFIT_BPS`; recompute net/bps from VWAP. | open |
| B2 | ✶ | HIGH | **Gas is a no-op and mis-modeled.** `gas_estimate=0` by default → "net of gas" does nothing. And gas is folded **per-set** then gated per-set, so a real `1000·$0.02 − $5` arb is rejected. | Nonzero default; gate/rank on total economics `size·(gross−fees) − gas`, not per-set. | **done (2026-06-29)** — gas modeled per-execution; gate/rank on total $ via `total_net_profit` + gas-adjusted bps |
| B3 | B | MED | **Overpriced-basket dual undetected** — `Σ NO < N−1` (buy every NO) is a structural edge we miss entirely. | Add the dual + property test (same exhaustiveness precondition as A1). | open |

## Tier C — The ranking / risk layer is largely cosmetic

| # | Str | Sev | Issue | Fix direction | Status |
|---|-----|-----|-------|---------------|--------|
| C1 | ✶ | HIGH | **`AT_RISK` is never assigned** → the default-on `exclude_at_risk_resolution` gate excludes nothing (dead safety filter). *Fix is a policy choice* (which categories are at-risk) — ties into C2. | Give `classify_market` an `AT_RISK` path; ideally fold into the probabilistic risk of C2. | open |
| C2 | ✶ | HIGH | **"Risk-adjusted" is a category sort, not a risk adjustment.** Lexicographic `(risk, bps, …)` → a 2 bps objective arb outranks a 900 bps politics arb; nothing discounts payoff by void/dispute probability. | Per-tier clean-resolution probability `p`; rank by `p·edge − (1−p)·loss`. Makes risk continuous + tunable. | open |
| C3 | ✶ | HIGH | **No winner's-curse / multiple-testing guard.** Sorting thousands of markets by raw bps surfaces the artifacts first (stale books, mislabeled outcomes; tiny-cost legs make bps explode). | Implausibility ceiling + corroboration (book freshness, two-sided depth) before top-ranking. | open |
| C4 | ✶ | MED-HIGH | **Backtest "would-be P&L" is upward-biased fiction** — sums `net·size` over all stored opps, re-counting persistent mispricings every pass, costless full capture, no realized-outcome tracking. | Dedupe to distinct economic opps; track realized resolution; label as upper bound. | open |
| C5 | ✶ | MED | **Annualization assumes continuous recycling** of a one-shot lock (overstates, rewards short-dated) and is **nearly vestigial** (3rd tiebreak). Ranking by bps also **ignores dollar scale**. | Decide the objective explicitly (the open `TESTING.md §6` question); fold horizon/scale into one score or drop it. | open |

## Tier D — Hardening / smaller

| # | Str | Sev | Issue | Fix direction | Status |
|---|-----|-----|-------|---------------|--------|
| D1 | D | MED | Hand-declared relations **bypass the §6 fingerprint gate** (docs claim `filters.py` enforces it; it doesn't). Wrong-direction/different-index relation → full-loss "lock". | Enforce fingerprint match in `add_relation` or `filters.py`; fix the doc drift. | open |
| D2 | B,C | MED* | Detectors **trust `clob_token_ids[0] == YES`**; a reversed-outcome market silently corrupts the identity. (*low confidence — convention usually holds.) | Validate `outcomes[0]` against an expected YES label / carry an explicit `yes_index`. | open |
| D3 | B, D | MED | **Multi-leg horizon should be `max(legs)`**, not first/either (bug-9 fixed the falsy-0; this is the deeper semantics). | Use `max(days)` across legs for held arbs. | open |
| D4 | C | MED | **Instant complement arbs are resolution-risk-gated/ranked** though they never reach resolution — can discard risk-free money. | Short-circuit `resolution_risk` to OBJECTIVE/n-a for `realizes=="instant"`; exempt from the AT_RISK drop. | **done** (2026-06-29) — `resolution_risk_for()` tags instant arbs OBJECTIVE |
| D5 | ✶ | MED/LOW | Multi-leg risk aggregated by `max` understates compounded void exposure (`1−Π(1−pᵢ)`); per-leg `min_order_size`/tick not enforced; no adverse-selection haircut; no deterministic final tiebreak. | Address alongside C2 (risk) / B1 (sizing). | open |

## Focus

Per "perfect one thing first," the candidate first target is the **NegRisk basket** (SPEC's
highest-value strategy and where A1/A2/A3/B1/B2 all converge) — make its "guaranteed $1"
*actually* guaranteed before touching the others. Decision pending (see the session summary).
