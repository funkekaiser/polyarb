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
| D3 | **Max-leg horizon** | held arbs annualize on `max(leg days)` over their own `condition_ids`, centralized in `make_opportunity` (capital locks until the latest leg resolves). |
| B2′ | **Leg-scaled gas (mechanism)** | `Snapshot.gas_for(n) = gas + gas_per_leg·n`; each detector charges for its own leg count. Conservative ceiling defaults (0.02/0.05 USDC). *Real Polygon numbers → desk.* |
| B2′-dyn | **Live gas oracle** | `GasClient` (Polygon Gas Station + CoinGecko POL/USD → USDC, TTL-cached, keyless) wired into the scanner behind `use_dynamic_gas` (OFF by default). Any oracle failure (incl. zero/negative values) → `GasUnavailable` → silent fallback to static config; never aborts a pass. Default path constructs no client and makes no network call. |
| C1-atom | **Conservative size (surface)** | `Opportunity.conservative_size` = min best-level depth across legs (`top_level_min_depth`), the pessimistic companion to the optimistic full-walk `executable_size`. Diagnostic only. *Whether ranking/filtering should use it → desk.* |
| A2 | **Void — partial** | closed-leg (post-) void handled by `live_partition`; `customLiveness>default→ELEVATED` (weak). **Core pre-resolution void still OPEN** → see A2-void. |
| — | **Process** | review-panel pattern added to CLAUDE.md; full doc cleanup; behavior-preserving refactor (`walk_and_size_buy_basket`, `live_partition`). |

---

## ON JONATHAN'S DESK — decisions awaiting your call (blocking nothing today)

Each is implemented to a safe/conservative default; full context lives in the tier tables (one
home per item — no duplicate prose). Awaiting only a judgment call:

- **D1** — fingerprint-gate policy for hand-declared relations (hard gate / honor-system / attestation). → *Tier D*
- **C1-atom-use** — should ranking ($-axis) + the `MIN_NOTIONAL` filter trust the optimistic `executable_size` or the conservative `conservative_size` (or a haircut)? → *Tier A (C1-atomicity-use)*
- **B2′-num** — accept the live gas oracle as source of truth, or measure + pin static gas numbers? → *Tier D*
- **A2-void** — accept the pre-resolution void residual, or invest in a curated void-prone denylist? → *Tier A (A2-void)*

## Decided / closed (no action)

- **C2** — probabilistic / risk-adjusted ranking: **deferred** (needs a void/dispute probability we can't measure; staying with guaranteed strategies).
- **§5** — opt-in partial basket: **shipped, off by default** (directional; never on the default scan path).
- **C5** — folded into **C3** (rank by absolute net $).
- **C4** — folded into **E1** (the realized-outcome ledger is the mechanism; backtest upper-bound labelling happens there).

---

## Open — Tier A: is the "guaranteed" money really safe?

| # | Str | Sev | Issue | Fix direction |
|---|-----|-----|-------|---------------|
| A2-void | B,D | HIGH | Pre-resolution void/50-50 for **live** legs — no reliable predictive signal in available data (`customLiveness` is window length, not void prob). | Curated void-prone source/category denylist (needs a live-API survey) or a payoff-haircut for held arbs; else accept as a documented residual gated by C1. |
| C1-atomicity-use | ✶ | HIGH (decision) | The conservative `conservative_size` is now **surfaced** (shipped), but ranking ($-axis) and the `MIN_NOTIONAL` filter still trust the optimistic `executable_size`. Switching them to the conservative size (or a survival-haircut blend) is a risk-appetite call. | **DESK** — Jonathan picks: keep optimistic / use conservative / haircut factor. Matters most pre-execution for honesty of rank+notional. |
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

(C4 — backtest "would-be P&L" upper-bound labelling — folded into **E1**; see Tier E and Decided/closed.)

## Open — Tier D: hardening / smaller

| # | Str | Sev | Issue | Fix direction |
|---|-----|-----|-------|---------------|
| D1 | D | MED | Hand-declared relations **bypass the §6 fingerprint gate** (RELATIONS.md fixed; code not). Wrong-direction relation → full-loss "lock". | Enforce fingerprint match in `add_relation`. |
| D2 | B,C | MED* | Detectors trust `clob_token_ids[0]==YES`; a reversed-outcome market corrupts the identity. (*low confidence.) | Validate `outcomes[0]` / carry an explicit `yes_index`. |
| B2′-num | ✶ | MED (decision) | Gas mechanism shipped with a conservative static ceiling (0.02/0.05 USDC) **and** an opt-in live oracle (B2′-dyn). Real measured Polygon/USDC numbers (merge/redeem + taker fills) would let us tighten the static knobs or trust the oracle. | **DESK** — Jonathan/ops: accept the live oracle, or measure once and pin `gas_estimate`/`gas_per_leg_estimate`. |
| D7-heartbeat | ✶ | LOW | The Docker healthcheck scrapes `/metrics` (a daemon thread) → it catches a dead/crash-looping process but **not a wedged scan loop**. No loop-progress liveness signal. | Emit a per-pass heartbeat (e.g. write last-pass mono/wall time to a small file in `/data` or a metrics gauge) and have the healthcheck assert it advanced within ~N intervals. |
| D5 | ✶ | MED/LOW | Multi-leg risk aggregated by `max` understates compounded exposure; per-leg `min_order_size`/tick not enforced; no deterministic final tiebreak. | Address alongside C-layer / sizing. |
| D6 | C | LOW | `walk_sell_legs` takes a scalar fee only (asymmetric with `walk_buy_legs`). | Accept `Decimal | Sequence[Decimal]` + reuse `_per_leg_rates`. |
| F2 | ✶ | LOW | The walks aren't property-tested directly (pure `*_profit` fns are off the runtime path). | Property-test `walk_buy_legs`/`walk_sell_legs` (monotone marginal, prefix-optimality, fee ≥ 0). |
| C-defer | C | LOW | Complement deferrals: greedy-walk vs threshold coupling; worst-fill `Leg.price` (Phase-5 executor); NegRisk merge routing + higher gas (Phase-5); 1e-28 VWAP rounding / min-size. | Mostly Phase-5 / negligible; revisit then. |

## Open — Tier E: realized-outcome tracking & evaluation (added 2026-06-30)

The natural next chunk *after* detection. Source: `docs/QUICK_THOUGHTS_OF_THE_DEV.md` (now
folded here). Today we record opportunities **at detection time only** and never learn how the
underlying markets actually resolved — so we can't compute realized P&L, audit whether
"guaranteed" was truly guaranteed, or measure a statistical edge. **E1 is the foundation; E2/E4
depend on it.** This is its own body of work, not a quick add.

| # | Str | Sev | Issue | Fix direction |
|---|-----|-----|-------|---------------|
| **E1** | ✶ | MED (foundation) | **No realized-outcome ledger.** Emitted opps aren't deduped to distinct economic events, and nothing fetches their eventual resolution → realized P&L is unknown; `backtest` (C4) is an upper-bound fiction. | Persist each emitted opp as a distinct economic event; a follow-up (read-only) job polls Gamma/Data for the resolution of its `condition_ids` and records realized payoff + P&L. Parent of **C4**; prerequisite for E2/E4. |
| **E2** | ✶ (C,B,D) | HIGH (audit) | **No alarm when a "guaranteed" arb settles negative.** A model-free lock can still go bad via void/50-50 (**A2-void**), an unfilled leg (execution), or a mis-declared relation (**D1/D2**) — silently. This is an audit of our core claim. | On top of E1: follow each emitted *instant/structural* opp to settlement; **alert** (notifier) if realized P&L < 0. Stretch: flag earlier, the moment the live book makes positive settlement unreachable (overlaps **E3**). |
| **E3** | ✶ | Phase-5 | **No live position monitor / edge-evaporation alert.** "Earlier, when it can't get positive anymore" assumes we hold a position and watch its book — an execution-side feature. | Defer to Phase 5: watch the live book of an open position; alert when the edge has evaporated. Depends on actually holding (or paper-trading) positions. |
| **E4** | §5, C | DEFERRED | **No edge-vs-luck test for probabilistic schemes.** Permutation / p-test to tell a real statistical edge from luck. Caveat: **we take no probabilistic bets yet** (§5 off, C2 deferred) → nothing to test until real or paper-traded directional bets exist. | After E1 + recorded directional bets: run permutation/bootstrap tests on realized P&L (is mean > 0 beyond chance?). Until then it would test an empty sample. Depends on **E1**; pairs with **§5/C2**. |

---

## Focus (next)

The model-free no-brainers from the C-layer and the four-item review are all **shipped**:
C3 (rank by $), C1 (dispute gate), D3 (max-leg horizon), B2′ (leg-scaled gas mechanism), and
C1-atom (conservative-size surface).

- **Remaining open, model-free (candidates for further bug-hunt/cleanup):** **A1-stale**,
  **A1-riskwt**, **A3-quiescence**, **M3-feefloor**, **C4** (backtest upper-bound labelling),
  **D2/D5/D6/F2** (small hardening). **D1** (fingerprint gate) is open but its absent-fingerprint
  policy is a **DESK** decision.
- **On Jonathan's desk (need input, see below):** D1 policy, C1-atomicity-use (which size
  ranking/notional trust), B2′-num (real gas numbers), A2-void (curated denylist or accept).
- **Deferred:** **C2** (probabilistic ranking — needs an unmeasurable probability). **§5** stays
  opt-in / off by default. **E4** (edge-vs-luck test — no probabilistic bets to test yet).
- **Next real chunk (post-detection):** **Tier E** — build the realized-outcome ledger (**E1**),
  then the guaranteed-slip alarm (**E2**). This is where evaluation/alerting lives; it's its own
  workstream, not a quick add. Wiring a notifier (currently `none`) is the cheap prerequisite for
  E2's alerts.
