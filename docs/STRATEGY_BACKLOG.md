# Strategy backlog

Open strategy/modeling work surfaced by the Opus "statistician committee" reviews, plus a
record of what's shipped. Line-level bugs live in `docs/TESTING.md §4`; the profit math in
`SPEC.md` and `docs/HEDGING.md`. Detailed committee narratives are in git history / commit
messages. Strategy tags: **C** = complement, **B** = NegRisk basket, **D** = dependency,
**✶** = cross-cutting. Principle: perfect one thing at a time; never fabricate profit.

---

## Shipped (committee-reviewed)

| # | What | Notes |
|---|------|-------|
| B1 | **Joint depth-walk sizing** | all detectors size via `walk_buy_legs`/`walk_sell_legs` (per-leg fees) with crossed-book + gas guards. |
| B2 | **Gas modeled per-execution** | gate/rank on total $ (`size·net − gas`), not per-set. |
| D4 | **Instant arbs → OBJECTIVE** | complement realizes before resolution, so resolution risk can't demote/exclude it. |
| A1 | **Basket exhaustiveness** | buy only a provably-complete partition; drop a closed leg only if its resolved YES proves it lost (~0), else skip; skip augmented/holes; ≥2 live legs. Also fixed a live bug (events with eliminations never emitted). |
| A3 | **Staleness net** | drop books older than `max_book_age_s` (CLOB last-change ts; default 900s). A gross-staleness/corruption net, *not* a fine freshness filter (quiescent≠stale). |
| B3 | **NO-basket dual** (`Σ NO < M−1`) | model-free hedge; **void-gated to OBJECTIVE legs** (the dual is uniquely fragile to a losing leg voiding — asymmetric vs the YES basket). |
| §5 | **Opt-in partial basket** | `PartialBasketDetector`, OFF by default, DIRECTIONAL-tagged (ranks below every structural arb). EV honestly labelled **optimistic, not a floor**; worst-case loss surfaced. |
| C3 | **Rank by absolute net $** | ranking sorts on `total_net_profit` (`size·net − gas`), not bps — real money rises, thin/low-volume artifacts sink (winner's curse). Risk tier primary, then $, then annualized. Subsumes C5. |
| C1 | **AT_RISK on active UMA dispute** | `umaResolutionStatuses` now parsed; an active *dispute* → AT_RISK → dropped by the default `exclude_at_risk` filter. Real safety gate for held arbs; instant complement exempt. (Not a probability.) |
| A2 | **Void — partial** | closed-leg (post-) void handled by `live_partition`; `customLiveness>default→ELEVATED` (weak). **Core pre-resolution void still OPEN** → see A2-void. |
| — | **Process** | review-panel pattern added to CLAUDE.md; full doc cleanup; behavior-preserving refactor (`walk_and_size_buy_basket`, `live_partition`). |

---

## Open — Tier A: is the "guaranteed" money really safe?

| # | Str | Sev | Issue | Fix direction |
|---|-----|-----|-------|---------------|
| A2-void | B,D | HIGH | Pre-resolution void/50-50 for **live** legs — no reliable predictive signal in available data (`customLiveness` is window length, not void prob). | Curated void-prone source/category denylist (needs a live-API survey) or a payoff-haircut for held arbs; else accept as a documented residual gated by C1. |
| C1-atomicity | ✶ | HIGH | Full-walk `executable_size` assumes an **atomic** multi-leg fill; real fills are sequential and legs move/vanish (adverse selection → partial unhedged position). Optimistic ceiling, worse as N/levels grow. | Report a conservative `top_level_size` alongside the walk and rank/filter on it; or a survival haircut growing in N. Couples with A3. |
| A3-quiescence | B,D | MED | The age-net can't distinguish a corrupt stale snapshot (#180) from a quiescent-but-valid book; any threshold trades thin-market coverage for staleness safety. | Targeted #180 detector: flag a book whose `hash` reverted or that shows the 0.01/0.99 corrupt pattern, instead of a blunt age cutoff. |
| A1-stale | B | MED | A *stale-closed* leg (Gamma says closed but still trading) has no book in the snapshot to reveal the staleness; A1 trusts `outcome_prices`. | Fetch a closed leg's book when its resolution is borderline; a live two-sided book on a "closed" market ⇒ stale metadata ⇒ skip. |
| A1-riskwt | ✶ | MED | A1 now (correctly) emits baskets from events with eliminations — disproportionately late-life, thin, stale-print. `#live/#total` and "Σ_live « 1" are unmodeled risk signals. | Surface `#live/#total` on the Opportunity; down-weight near-fully-resolved baskets (pairs with C1/C3). |
| M3-feefloor | B,D | MED | Parabolic taker fee `C·r·p·(1−p)` → 0 at p→0/1 where longshot legs live; if the live schedule has a floor/minimum, edge is overstated. | Verify a live per-order fee floor against `API_NOTES`; if it exists, add it per leg. |

## Open — Tier C: ranking / risk layer (revised 2026-06-30)

(C3 rank-by-$ and C1 dispute-gate are **shipped** — see above. C5 folded into C3.)

| # | Str | Sev | Issue | Decision / direction |
|---|-----|-----|-------|----------------------|
| C1+ | ✶ | LOW | Optional extension of the shipped C1 dispute gate: a curated subjective-/manipulable-source denylist. | Only if a credible curated list emerges; the active-dispute signal is the real one. Don't guess categories. |
| C2 | ✶ | — | "Risk-adjusted" ranking by clean-resolution probability `p·edge − (1−p)·loss`. | **DEFERRED (2026-06-30, Jonathan): do not implement.** It needs a void/dispute probability we can't measure (A2), and we're staying with guaranteed strategies — no probabilistic ranking for now. |
| C4 | ✶ | MED-HIGH | Backtest "would-be P&L" is upward-biased fiction — re-counts persistent mispricings every pass, costless full capture, no realized-outcome tracking. | Dedupe to distinct economic opps; track realized resolution; label as an upper bound. |

## Open — Tier D: hardening / smaller

| # | Str | Sev | Issue | Fix direction |
|---|-----|-----|-------|---------------|
| D1 | D | MED | Hand-declared relations **bypass the §6 fingerprint gate** (RELATIONS.md fixed; code not). Wrong-direction relation → full-loss "lock". | Enforce fingerprint match in `add_relation`. |
| D2 | B,C | MED* | Detectors trust `clob_token_ids[0]==YES`; a reversed-outcome market corrupts the identity. (*low confidence.) | Validate `outcomes[0]` / carry an explicit `yes_index`. |
| D3 | B,D | MED | Multi-leg horizon should be `max(legs)` (capital locked until all required legs resolve); basket uses first-known, dependency B-then-A. | Use `max(days)` across the legs whose resolution is required. |
| B2′ | ✶ | MED | One fixed `gas` under-charges high-N baskets (N taker orders + merge/redeem). | `gas = base + per_leg·N`. |
| D5 | ✶ | MED/LOW | Multi-leg risk aggregated by `max` understates compounded exposure; per-leg `min_order_size`/tick not enforced; no deterministic final tiebreak. | Address alongside C-layer / sizing. |
| D6 | C | LOW | `walk_sell_legs` takes a scalar fee only (asymmetric with `walk_buy_legs`). | Accept `Decimal | Sequence[Decimal]` + reuse `_per_leg_rates`. |
| F2 | ✶ | LOW | The walks aren't property-tested directly (pure `*_profit` fns are off the runtime path). | Property-test `walk_buy_legs`/`walk_sell_legs` (monotone marginal, prefix-optimality, fee ≥ 0). |
| C-defer | C | LOW | Complement deferrals: greedy-walk vs threshold coupling; worst-fill `Leg.price` (Phase-5 executor); NegRisk merge routing + higher gas (Phase-5); 1e-28 VWAP rounding / min-size. | Mostly Phase-5 / negligible; revisit then. |

---

## Focus (next)

The remaining work splits cleanly by whether it touches guaranteed money:

- **Done (safe-money, model-free):** **C3** (rank by absolute net $) and **C1** (AT_RISK on
  active UMA dispute) — shipped.
- **Next, cheap correctness (all model-free):** **D3** (max-leg horizon for held arbs),
  **D1** (enforce the fingerprint gate on hand-declared relations), **C1-atomicity**
  (a conservative size alongside the optimistic full-walk ceiling), **B2′** (per-leg-count gas).
- **Deferred / not pursuing now:** **C2** (probabilistic risk-adjusted ranking — needs a
  probability we can't measure). **A2-void** core stays a documented residual until a real
  signal exists. **§5** stays opt-in / off by default.
