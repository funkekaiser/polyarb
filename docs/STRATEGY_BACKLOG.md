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
| B1 | B,C,D | HIGH | **Sizing never walks past top-of-book** — each detector passes the *best* price as the depth limit, so `executable_size` = thinnest leg's *top level only*. Systematically under-sizes and drops real basket arbs. (`TESTING.md §5`'s "upper bound" note is backwards — it's a conservative lower bound.) | Walk legs jointly; accumulate size while combined VWAP still clears `MIN_PROFIT_BPS`; recompute net/bps from VWAP. | **done (2026-06-29)** — all three detectors now joint-depth-walk via `walk_buy_legs`/`walk_sell_legs` (per-leg fee rates) with crossed-book + gas-realizability guards; verified by a 3-seat Opus panel (no correctness bugs). *Caveat raised: the full-walk size assumes atomic multi-leg fills — see C1-atomicity below.* |
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

## Complement bulletproofing — committee re-check (2026-06-29)

We chose to perfect **complement** first (it's immune to the Tier-A resolution risks). After
D4/B2/B1, a 2-seat Opus committee confirmed the **identity + walk math is correct**. Outcomes:

**Fixed (this pass):**
- `accepting_orders` now gates discovery — paused/halting markets no longer emit unfillable arbs.
- Crossed-book guard — skip a market if either YES/NO book has `bid ≥ ask` (stale/erroneous data).
- Non-positive prices filtered in the depth-walk (bad-payload guard).
- Gas-negative emission suppressed in the detector (guard, not `continue` — keeps under/over independent).

**Verified, no change needed:**
- **NegRisk-constituent merge** (committee #4): confirmed from `NegRiskAdapter.sol` that YES+NO of one
  constituent merges 1:1 to $1 with **no protocol fee** → complement's cost model is correct; do NOT
  exclude NegRisk markets. (Recorded in `docs/API_NOTES.md`, dated.)

**Deferred (conservative false-negatives / Phase-5 — do NOT fabricate profit):**

| # | Issue | Why deferred | Fix direction |
|---|-------|--------------|---------------|
| C-defer-1 | Greedy walk dilutes blended bps below `MIN_PROFIT_BPS` → whole opp filtered | Drops money, never fabricates; fix couples walk to threshold+gas | Size to max `total_net` s.t. aggregate bps ≥ threshold |
| C-defer-2 | Cross-leg staleness (A3 also applies to complement) | Broader A3 feature | Reject when the two legs' `timestamp_ms` skew beyond a budget; prefer WS |
| C-defer-3 | `Leg.price` is VWAP, not worst-fill | Phase-5 executor concern; detection economics are correct | Carry worst-acceptable price / level schedule for the executor |
| C-defer-4 | NegRisk merge routing + higher gas | Phase-5 execution only | Route merge via `NegRiskAdapter`; higher per-exec gas constant for `negRisk` |
| C-defer-5 | `min_order_size`/tick not enforced in the walk (D5); 1e-28 VWAP rounding | Negligible / venue-min concern | Floor to `min_order_size`; thread exact walk totals if ever needed |

## Basket/dependency B1 — panel re-check (2026-06-29, second hardening pass)

After bringing negrisk_basket + dependency to complement's bar (B1 done), a 3-seat Opus panel
(profit-identity math · execution realism · numerical fidelity) plus an adversarial code
reviewer audited the change. **Verdict: the depth-walk math is faithful and the identities are
sound; no correctness bugs.** The panel strongly **corroborated the existing Tier-A worklist**
as the real remaining risk (A1 exhaustiveness, A2 void/50-50, A3 staleness) and refined a few
items. New/refined entries:

| # | Str | Sev | Issue | Fix direction | Status |
|---|-----|-----|-------|---------------|--------|
| B2′ | ✶ | MED-HIGH | **Gas is one fixed charge, but a basket is N taker orders + merge/redeem.** A single `snap.gas` under-charges high-N baskets — exactly the highest-value opps — biasing toward false positives as N grows. (Refines the now-done B2.) | `gas = base + per_leg·N`; thread leg-count into the per-execution estimate. | open |
| C1-atomicity | ✶ | HIGH | **Full-walk size assumes an atomic multi-leg fill.** Book-mechanical leg independence ≠ execution independence: fills are sequential, and between lifting leg 1 and leg N the other legs move/vanish (adverse selection preferentially completes the *bad* legs → partial unhedged position). Full-walk size is an **optimistic ceiling**, worse as N and levels grow. | Report a conservative `top_level_size` alongside the walk and filter/rank on it; or apply a survival haircut growing in N/levels. Couples with A3. | open |
| M3-feefloor | B,D | MED | **Parabolic taker fee `C·r·p·(1−p)` → 0 at p→0/1**, where basket/dependency longshot legs live. If the live schedule has a per-order floor/minimum/round-up, the model under-charges exactly those legs and inflates their edge. | Verify against `docs/API_NOTES.md`; if a floor exists, add it per leg. | open |
| D3′ | B,D | MED | **Multi-leg horizon confirmed wrong**: dependency annualizes off B's horizon (fallback A), basket off the *first* known leg — should be `max(legs)` (capital locked until all required legs resolve). (Sharpens D3.) | Use `max(days)` across the legs whose resolution is required for the guaranteed payoff. | open |
| F2-proptest | ✶ | LOW | **Pure profit fns (`*_profit`) are no longer on the detector runtime path** (detectors compute inline via the walk); only example-tested. | Property-test `walk_buy_legs`/`walk_sell_legs` directly (monotone marginal, prefix-optimality, fee ≥ 0). | open |
| D6 | C | LOW | **`walk_sell_legs` takes a scalar fee only** (asymmetric with `walk_buy_legs`); latent fee error if reused for cross-market sells. | Accept `Decimal | Sequence[Decimal]` + reuse `_per_leg_rates`. | open |

Addressed in this pass (test/robustness, committed): discriminating per-leg-fee alignment
tests (walk + both detectors), exact-gas-boundary test, `walk_buy_legs([])` returns zero,
and the corrected `TESTING.md §5` size note (optimistic-ceiling caveat).

## Focus (next, after complement)

B1/B2 are now done across all three detectors. The next strategy to perfect is the **NegRisk
basket** (SPEC's highest-value strategy, where A1/A2/A3 converge) — make its "guaranteed $1"
*actually* guaranteed (**exhaustiveness A1 + void/50-50 A2 + staleness A3**) before touching
dependency. The panel rates A1 (exhaustiveness) the most dangerous open correctness gap: an
unlisted "other/none" outcome turns a "free" basket into a guaranteed loss.
