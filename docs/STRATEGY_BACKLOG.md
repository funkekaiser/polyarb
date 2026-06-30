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
| A3-q | **Corrupt-book gate (partial)** | `is_corrupt_book` (`pricing/book_quality.py`) flags the #180 degenerate `bid≤0.01 ∧ ask≥0.99` extreme spread and is gated alongside `is_crossed` in complement + NegRisk YES/dual detectors. Stateless; only ever a false-NEGATIVE (skip), never a fabricated arb. **Hash-revert half still open** (needs cross-pass state + `OrderBook.hash`) → see A3-quiescence. |
| D2 | **YES-index identity (partial)** | `Market.yes_index` detects a reversed `["No","Yes"]` outcome pair; `yes_token_id`/`no_token_id`/`yes_outcome()` route through it so the buy/sell legs use the right tokens. Canonical `["Yes","No"]`/no-outcomes paths byte-identical. **Residual:** `live_partition` resolved-price check (`negrisk_basket.py:95`) + cosmetic leg labels still read `[0]` → see D2-residual. |
| D6 | **Per-leg sell fees** | `walk_sell_legs` now accepts `Decimal \| Sequence[Decimal]` via `_per_leg_rates`, symmetric with `walk_buy_legs`; scalar callers (complement) broadcast unchanged. |
| F2 | **Walks property-tested** | `walk_buy_legs`/`walk_sell_legs` covered by Hypothesis property tests: fee≥0, prefix-optimality, monotone marginal cost/proceeds, no spurious inclusion, D6 scalar≡sequence regression. |
| D7 | **Loop heartbeat** | `Scanner.run` atomically writes a per-pass timestamp to `heartbeat_path` (default None = off) + a `polyarb_last_pass_timestamp_seconds` gauge; new `polyarb healthcheck` CLI (fresh ≤ `max(2·interval,120s)`) replaces the `/metrics` scrape as the Docker liveness probe — catches a wedged loop, not just a dead process. |
| — | **Process** | review-panel pattern added to CLAUDE.md; full doc cleanup; behavior-preserving refactor (`walk_and_size_buy_basket`, `live_partition`). Parallel-worktree hardening batch (A3-q/D2/D6/F2/D7). |

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

## Strategy direction — small-edge tier (DEFERRED 2026-06-30)

Jonathan's instinct: as a small player, chase *volume of small REAL edges* rather than compete
for big ones; the enemy is false positives, not size (filter on reality, rank on size). Sound
in principle — but a **live recon (gasless, thresholds=1, 600 mkts, 15 passes)** showed it's
premature *now*:

- The **instant** small edges needed for "$1/min" (complement merge/split) **don't exist** —
  books are spread-locked at ±~10 bps (no complement arb on the whole board).
- The small edges that *do* exist are **held-to-resolution baskets**: ~$22 notional, ~40 bps,
  **~184 days** to resolution → annualized **~0.8%/yr** with resolution risk. Junk capital
  efficiency; the `$50 MIN_NOTIONAL` + annualized-aware rank already (correctly) filter them out.

**Revisit only when the enablers exist:** (a) **websocket streaming** (D-ws) to catch *instant*
small transients polling misses; (b) **false-positive hardening** so a penny-edge is trustworthy
— A3-quiescence (#180 corrupt 0.01/0.99 pattern), A1-stale, per-leg **min-order-size enforcement
(D5)**, dispute/void gating; (c) **gas model confirmed** (gasless relayer ⟹ ~$0; B2′); (d)
eventually **automated execution** (Phase 5) — $1/min can't be hand-fired. Until then: stay on
the sensible/"big" tier. (Memory: small-edge-strategy.)

---

## Open — Tier A: is the "guaranteed" money really safe?

| # | Str | Sev | Issue | Fix direction |
|---|-----|-----|-------|---------------|
| A2-void | B,D | HIGH | Pre-resolution void/50-50 for **live** legs — no reliable predictive signal in available data (`customLiveness` is window length, not void prob). | Curated void-prone source/category denylist (needs a live-API survey) or a payoff-haircut for held arbs; else accept as a documented residual gated by C1. |
| C1-atomicity-use | ✶ | HIGH (decision) | The conservative `conservative_size` is now **surfaced** (shipped), but ranking ($-axis) and the `MIN_NOTIONAL` filter still trust the optimistic `executable_size`. Switching them to the conservative size (or a survival-haircut blend) is a risk-appetite call. | **DESK** — Jonathan picks: keep optimistic / use conservative / haircut factor. Matters most pre-execution for honesty of rank+notional. |
| A3-quiescence | B,D | LOW (residual) | **Extreme-spread half SHIPPED** (`is_corrupt_book`, gated in all 3 buy/sell detectors). Remaining: a book whose `hash` *reverted* across passes (a stale snapshot that still has a plausible mid) isn't caught by the stateless predicate. | Add an `OrderBook.hash` field + a per-token last-hash map in `Scanner`; flag a token whose hash reverts to an earlier value. Needs cross-pass state. |
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
| D2-residual | B,C | LOW | **Core SHIPPED** (`Market.yes_index`; buy/sell legs use the right tokens). Residual: `live_partition`'s closed-leg resolved-price check (`negrisk_basket.py:95`, `outcome_prices[0]`) and cosmetic leg labels (`outcomes[0]` in negrisk/partial detectors) still assume index 0 → a reversed *and closed* constituent could misfire the elimination gate. | Route those reads through `market.yes_index`; add a reversed-closed-leg regression test. Small, but touches detector files. |
| B2′-num | ✶ | MED (decision) | Gas mechanism shipped with a conservative static ceiling (0.02/0.05 USDC) **and** an opt-in live oracle (B2′-dyn). Real measured Polygon/USDC numbers (merge/redeem + taker fills) would let us tighten the static knobs or trust the oracle. | **DESK** — Jonathan/ops: accept the live oracle, or measure once and pin `gas_estimate`/`gas_per_leg_estimate`. |
| D5 | ✶ | MED/LOW | Multi-leg risk aggregated by `max` understates compounded exposure; per-leg `min_order_size`/tick not enforced; no deterministic final tiebreak. | Address alongside C-layer / sizing. |
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

## Roadmap — ordered execution plan (updated 2026-06-30)

**State:** read-only monitor live in Docker — sensible tier (30 bps / $50), 600-market coverage,
hardened container. Diagnostics + coverage-widening shipped; recon done. Penny/small-edge tier
**deferred** (see "Strategy direction" above — recon-killed for now). Work the items below in order.

1. **Notifier wiring (Discord)** — *built.* `DiscordNotifier` (formatted embed) shipped; set
   `NOTIFIER=discord` + `NOTIFIER_URL=<channel webhook>` in the compose env so real opps actually
   alert (spec's "→ alert" is otherwise silent). Pending only Jonathan's Discord webhook URL.
   (ntfy/telegram deferred — fall back to `none`.)
2. **Dependency-relation workflow** — *the big near-term build.* Auto-**propose** candidate
   relations from market *structure* (temporal/numeric ladders, nesting DAGs — never free-text),
   **verify** each (resolution-fingerprint + adversarial committee hunting an A∧¬B scenario), then
   **register only verified** ones. Activates the dormant dependency detector with no manual
   curation. Resolves **D1** (fingerprint policy) as part of it. Gate + committee before commit.
3. **Websocket streaming** — wire `ws.py` into the scan loop (in-memory books from deltas):
   real-time detection + far less CPU/IO than re-fetching ~924 books/pass. The only way to catch
   instant transients. After #2.
4. **False-positive hardening** — *partially shipped* (A3-quiescence extreme-spread predicate, D2
   yes-index, D6, F2, D7-heartbeat — parallel-worktree batch). Remaining: A3 hash-revert,
   D2-residual, A1-stale, per-leg min-order-size (D5), A1-riskwt, M3-feefloor. Quality now, *and*
   the prerequisite for revisiting the small-edge tier.
5. **Realized-outcome ledger (E1) → guaranteed-slip alarm (E2)** — the evaluation layer (did
   "guaranteed" really pay?). Its own workstream.

**Deferred:** small-edge tier (needs #3+#4+execution), **C2** (probabilistic ranking), **§5**
(opt-in/off), **E4** (no probabilistic bets yet). **Desk decisions still open:** D1 (handled in #2),
C1-atom-use, B2′-num / gas-wallet path, A2-void.

### Dependency-workflow design seed (from subsystem mapping, 2026-06-30)

So #2 can be built without re-mapping:
- **The generators are DONE and already enforce the §6 fingerprint gate — do NOT rebuild them.**
  `generate_ladder_relations(tags)` + `generate_dag_relations(tags, edges)` turn `MarketTags` into
  safe `Relation`s. **The only missing piece is a PROPOSER** reading live `Event`/`Market` →
  emitting `MarketTags`. (`TAG_REGISTRY`/`SEED_RELATIONS` start empty → detector is dormant.)
- **Start with BY_DATE temporal ladders:** `Market.end_date` is a structured field → `bound`
  directly; earlier deadline = antecedent. Safest, zero text inference.
- **Hardest sub-problem — `resolution_fingerprint`:** there is NO API field for it; two markets
  only ladder if fingerprints match. Derive a conservative fingerprint (underlying + settlement
  source/cutoff) AND gate every proposed pair through an **adversarial committee** that attests
  "same resolution + A⇒B truly holds" (hunts an A∧¬B scenario). Reject on any doubt.
- **Threshold ladders** (GTE/LTE) need parsing `group_item_title`/`question` (borderline text) —
  do AFTER BY_DATE, with the committee as the safety net.
- **D1:** the proposer→generator path is naturally gated; the only bypass is hand-called
  `add_relation()` (no fingerprint arg). Either enforce a fingerprint there or never hand-call it.
- **Persistence:** registries are in-process (reset per run) — add a store so verified tags
  survive restarts. Consumer `dependency.py` TRUSTS direction — correctness is 100% the proposer's.
