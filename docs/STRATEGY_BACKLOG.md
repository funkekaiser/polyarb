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
| B2′ | **Leg-scaled gas (mechanism)** | `Snapshot.gas_for(n) = gas + gas_per_leg·n`; each detector charges for its own leg count. |
| B2′-num | **Gas default → 0 (relayer reality)** | DECIDED 2026-07-01 (3-lens committee unanimous). Relayer (proxy/Safe) makes true user gas ≈$0; the old 0.02/0.05 ceiling over-charged raw Polygon gas and suppressed real small multi-leg edges (~104 bps on a 10-leg $50 basket). Defaults now 0; the ceiling + `use_dynamic_gas` oracle stay one flag away as raw-EOA / Phase-5 insurance. |
| B2′-dyn | **Live gas oracle** | `GasClient` (Polygon Gas Station + CoinGecko POL/USD → USDC, TTL-cached, keyless) wired into the scanner behind `use_dynamic_gas` (OFF by default). Any oracle failure (incl. zero/negative values) → `GasUnavailable` → silent fallback to static config; never aborts a pass. Default path constructs no client and makes no network call. **Committee: keep OFF** — as source-of-truth it prices gas the relayer user never pays and can under-charge → false positive. |
| A2-void | **Dependency void-gate + held relabel** | DECIDED 2026-07-01 (3-lens committee). A thin, held-to-resolution dependency lock is wiped by one leg's 50-50 void, so — mirroring the NO-dual — the detector now emits only when **both** legs resolve on an OBJECTIVE source (`classify_market == OBJECTIVE`). Held arbs relabeled "guaranteed modulo void"; complement stays truly void-immune. **No denylist** (not constructible read-only; guessing categories forbidden). Void is unhedgeable on-market → structural mitigation is preferring `realizes="instant"` arbs. |
| D2-residual | **Reversed closed-leg reads** | SHIPPED (`cf2a532`). `live_partition` resolved-price check + leg labels route through `market.yes_index`; no `[0]` index assumptions remain in detectors. Regression: `test_reversed_closed_eliminated_drops_correctly`. |
| C1-atom | **Conservative size (surface)** | `Opportunity.conservative_size` = min best-level depth across legs (`top_level_min_depth`), the pessimistic companion to the optimistic full-walk `executable_size`. Diagnostic only. *Whether ranking/filtering should use it → desk.* |
| A2 | **Void — partial** | closed-leg (post-) void handled by `live_partition`; `customLiveness>default→ELEVATED` (weak). **Core pre-resolution void still OPEN** → see A2-void. |
| A3-q | **Corrupt-book gate (partial)** | `is_corrupt_book` (`pricing/book_quality.py`) flags the #180 degenerate `bid≤0.01 ∧ ask≥0.99` extreme spread and is gated alongside `is_crossed` in complement + NegRisk YES/dual detectors. Stateless; only ever a false-NEGATIVE (skip), never a fabricated arb. **Hash-revert half still open** (needs cross-pass state + `OrderBook.hash`) → see A3-quiescence. |
| D2 | **YES-index identity (partial)** | `Market.yes_index` detects a reversed `["No","Yes"]` outcome pair; `yes_token_id`/`no_token_id`/`yes_outcome()` route through it so the buy/sell legs use the right tokens. Canonical `["Yes","No"]`/no-outcomes paths byte-identical. **Residual:** `live_partition` resolved-price check (`negrisk_basket.py:95`) + cosmetic leg labels still read `[0]` → see D2-residual. |
| D6 | **Per-leg sell fees** | `walk_sell_legs` now accepts `Decimal \| Sequence[Decimal]` via `_per_leg_rates`, symmetric with `walk_buy_legs`; scalar callers (complement) broadcast unchanged. |
| F2 | **Walks property-tested** | `walk_buy_legs`/`walk_sell_legs` covered by Hypothesis property tests: fee≥0, prefix-optimality, monotone marginal cost/proceeds, no spurious inclusion, D6 scalar≡sequence regression. |
| D7 | **Loop heartbeat** | `Scanner.run` atomically writes a per-pass timestamp to `heartbeat_path` (default None = off) + a `polyarb_last_pass_timestamp_seconds` gauge; new `polyarb healthcheck` CLI (fresh ≤ `max(2·interval,120s)`) replaces the `/metrics` scrape as the Docker liveness probe — catches a wedged loop, not just a dead process. |
| — | **Process** | review-panel pattern added to CLAUDE.md; full doc cleanup; behavior-preserving refactor (`walk_and_size_buy_basket`, `live_partition`). Parallel-worktree hardening batch (A3-q/D2/D6/F2/D7). |
| C1-atom-use | **Filter+rank on conservative size** | committee (2-1) + desk: `MIN_NOTIONAL` gate and the $-rank now act on `Opportunity.decision_size` (conservative best-level depth, `is None`→optimistic fallback); `executable_size`/`total_net_profit` kept as the surfaced optimistic ceiling. Honest floor + winner's-curse-free rank for a small non-atomic taker; applies to the streamed path too (R4). |
| A1-riskwt | **Live/total surfaced + soft rank tiebreak** | `Opportunity.live_count/total_count`; ranking's lowest-priority key prefers fuller baskets, clamped so it can never reorder real money. |
| **WS-default** | **WebSocket is the DEFAULT read path** (2026-07-01) | `streaming_enabled=True`; REST poll demoted to the resync/backup. Committee-reviewed (3 lenses, all SAFE/none blocking) + 2× worktree bug-hunt; **verified live in Docker** (single connection, live deltas flowing, graceful SIGTERM shutdown). |
| **R5** | **Stall watchdog** | per-message `asyncio.wait_for(anext)` deadline (`ws_stall_timeout_s`, def 60s) force-drops + reconnects a connected-but-silent feed (`ws_stalls` metric) so a dead feed can't silently degrade to a 60s poll. |
| **R6** | **Dynamic (un)subscribe, no reconnect** | `ws.stream()` control-queue select loop forwards subscribe/unsubscribe ops on the live socket (API_NOTES §WS); `set_tokens` diffs the discovery set + evicts dropped from the cache. |
| **R2** | **Streaming freshness guard** | `fresh_books()`/`scoped_fresh_books()` (loop-monotonic per-token) drop feed-silent tokens at detect time. Default `ws_freshness_s=90` ≥ `ws_resync_interval_s=60`+margin (committee fix: a shorter window blinked out quiescent tokens). Safety net atop R1. |
| **R8** | **Streaming metrics + stream-aware healthcheck** | `ws_last_message`(true-delta-only) / `ws_last_resync` / `ws_reconnects` / `ws_stalls` / `ws_resyncs` / `ws_resync_errors` / `ws_tracked_tokens` / `ws_skipped`. WS-heartbeat pulses on message-or-resync (and when idle-with-no-tokens); healthcheck fails when the cache is frozen even while the scan loop pulses. |
| **WS-maxsize** | **Live WS frame cap fix** | `websockets` 1 MiB default closed the connection on every connect (1009 MESSAGE_TOO_BIG — Polymarket's initial-dump is ~1.65 MiB/390 tokens); raised to 64 MiB (`WS_MAX_MESSAGE_BYTES`). **Caught by live Docker verification**; recorded in API_NOTES (dated). |
| **R-hardening** | **Bug-hunt + committee fixes** | in-flight-resync no longer resurrects an evicted token (both hunters); `ws_factory` failure backs off instead of crashing run(); best-effort streaming-init; first-wake resync clock-independent; `pytest-socket` now hard-enforces the offline-test constraint. |

---

## ON JONATHAN'S DESK — decisions awaiting your call (blocking nothing today)

Each is implemented to a safe/conservative default; full context lives in the tier tables (one
home per item — no duplicate prose). Awaiting only a judgment call:

- **D1** — fingerprint-gate policy for hand-declared relations (hard gate / honor-system / attestation). Leaning **attestation**; deferred into the dependency-relation workflow (which subsumes it). → *Tier D*
- ~~**C1-atom-use**~~ — **DECIDED 2026-07-01 (committee 2-1 + desk): filter+rank on the conservative `decision_size`.** Shipped.
- ~~**B2′-num**~~ — **DECIDED 2026-07-01 (3-lens committee unanimous + desk): gas default → 0 (relayer reality).** Oracle stays shipped-but-OFF and the conservative ceiling is one config flag away, as insurance for a future raw-EOA / Phase-5 path. Shipped.
- ~~**A2-void**~~ — **DECIDED 2026-07-01 (3-lens committee + desk): no denylist** (not constructible read-only) **; extend the OBJECTIVE-source gate to the dependency detector; relabel held arbs "guaranteed modulo void".** Void is unhedgeable on-market — the structural "hedge" is preferring `realizes="instant"` arbs. Void-rate measurement + any haircut deferred to E1/E2. Dependency gate shipped.

## Decided / closed (no action)

- **M3-feefloor** — **CLOSED 2026-06-30 (live-verified):** the `feeSchedule` is `{exponent,rate,takerOnly,rebateRate}` with **no per-order floor/minimum** across all fee types, so the parabolic taker fee is correct (not a gap). Trip-wire test pins it; API_NOTES records the finding.
- **C1-atom-use** — **DECIDED 2026-07-01 (committee 2-1 + desk):** filter + rank on the conservative `decision_size`; optimistic ceiling stays surfaced. Shipped (see Shipped table).
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
| A2-void | — | — | **DECIDED 2026-07-01 → Shipped table (A2-void row).** Dependency void-gate shipped; no denylist; measurement/haircut deferred to E1/E2. Residual pre-resolution void on held basket arbs is an accepted, documented residual (E2 is the settle-negative backstop). | — |
| A3-quiescence | B,D | LOW (residual) | **Extreme-spread half SHIPPED** (`is_corrupt_book`, gated in all 3 buy/sell detectors); **hash-revert SHIPPED for the streaming cache** (`OrderBook.hash` + per-token deque in `bookcache.py`). Residual: the pure-REST fallback path has no cross-pass hash tracking. | **ACCEPTED as residual under WS-first (2026-07-01):** streaming is the default and its cache already carries hash-revert; the uncovered path runs only when streaming is explicitly disabled. Revisit only if the REST fallback becomes load-bearing again. |
| A1-stale | B | MED | A *stale-closed* leg (Gamma says closed but still trading) has no book in the snapshot to reveal the staleness; A1 trusts `outcome_prices`. | Fetch a closed leg's book when its resolution is borderline; a live two-sided book on a "closed" market ⇒ stale metadata ⇒ skip. |

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
| D1 | D | MED | Hand-declared relations **bypass the §6 fingerprint gate** (RELATIONS.md fixed; code not). Wrong-direction relation → full-loss "lock". | Leaning **attestation** (`add_relation` requires an explicit fingerprint-verified affirmation). Deferred into the dependency-relation workflow, which subsumes it. |
| D5 | ✶ | MED/LOW | Multi-leg risk aggregated by `max` understates compounded exposure; per-leg `min_order_size`/tick not enforced; no deterministic final tiebreak. | Address alongside C-layer / sizing. |
| C-defer | C | LOW | Complement deferrals: greedy-walk vs threshold coupling; worst-fill `Leg.price` (Phase-5 executor); NegRisk merge routing + higher gas (Phase-5); 1e-28 VWAP rounding / min-size. | Mostly Phase-5 / negligible; revisit then. |

## Open — Tier D-ws: streaming polish (committee, 2026-07-01 — all NON-BLOCKING)

Surfaced by the streaming-default committee/bug-hunt. The migration shipped SAFE; these are
scaling/observability refinements, several needing **live measurement** at the 600-market default.

| # | Sev | Issue | Fix direction |
|---|-----|-------|---------------|
| WS-atomicity | MED | R1 confirm's per-leg REST fetches are non-atomic and are now the *sole* integrity barrier; a wide basket's legs can reflect slightly different moments. | Mitigated today by the 30 bps margin + tight freshness (a sub-tick skew can't flip a real-margin arb). Add a confirm pass/fail-rate metric to watch basket-confirm health; keep `ws_freshness_s` tight. |
| WS-resync-burst | MED | The full resync bursts all ~1200 tracked tokens against the shared `/book` bucket every 60s, competing with latency-critical R1 confirm reads (R7 sharing is correct for quota, but timing-adverse). | Trickle/jitter the full resync across the interval (batched) rather than one gather burst; or give resync a lower effective sub-rate. Needs live tuning. |
| WS-confirm-cap | LOW | A cache-corruption storm → many phantom candidates → confirm REST volume spikes on the shared limiter, potentially starving real confirmations. | Per-pass confirm cap + a candidates-seen-vs-confirmed metric; a spike signals cache degradation → force resync/alert. |
| WS-evict-hysteresis | LOW | Per-pass `set_tokens` evicts a token the moment it leaves the discovery cap, discarding its accumulated WS book state + A3 hash-revert history; a token oscillating around the cap thrashes. | Defer eviction with a grace period (drop only after N consecutive absent discoveries); and/or make discovery ordering deterministic so the cap slices the same set. |
| WS-quiet-churn | LOW | The stall timer resets only on an *applied* message, not WS ping/pong; a genuinely quiescent board would reconnect every `ws_stall_timeout_s`. Not a real risk at 1200 tokens (60s of total silence ⇒ dead), but a noise floor on `ws_reconnects`. | Treat any received frame as liveness, or scale the timeout with token count. Document the noise floor. |

## Open — Tier E: realized-outcome tracking & evaluation (added 2026-06-30)

The natural next chunk *after* detection. Source: `docs/QUICK_THOUGHTS_OF_THE_DEV.md` (now
folded here). Today we record opportunities **at detection time only** and never learn how the
underlying markets actually resolved — so we can't compute realized P&L, audit whether
"guaranteed" was truly guaranteed, or measure a statistical edge. **E1 is the foundation; E2/E4
depend on it.** This is its own body of work, not a quick add.

| # | Str | Sev | Issue | Fix direction |
|---|-----|-----|-------|---------------|
| **E1** | ✶ | **SHIPPED 2026-07-01** | Realized-outcome ledger, end to end. `economic_fingerprint` + `economic_events` table dedupe re-detections to distinct events (`sinks/store.py`); pure `engine/settlement.py` computes realized payoff/P&L from resolved token prices with void detection (off-{0,1} → void — the A2-void measurement); read-only `poll_settlements` polls Gamma by condition_id and writes realized P&L back, exposed as `polyarb settle` **and** a slow in-scanner cadence (`settle_interval_seconds`, default 1h). Gamma `condition_ids` filter flagged PENDING live verification (API_NOTES). **E1-d SHIPPED:** `polyarb backtest` prints a realized-ledger summary (pending/settled/void, realized P&L, win rate, worst realized loss) via `summarize_ledger` — C4 is now truth, not fiction. | **Next: E2** (alert when a settled event's realized P&L < 0 — the void backstop; the ledger already stores it). |
| **E2** | ✶ (C,B,D) | **SHIPPED 2026-07-01** | The audit alarm on the core claim. `poll_settlements` fires a notifier alert when a **structural** lock settles with realized P&L < 0 — catching void/50-50 (A2-void), a mis-declared relation (D1/D2), or an unfilled leg. Partial baskets excluded (directional EV). Alerts once per event (settles once, leaves the pending set). `Notifier.alert(title, body)` on Null/Webhook/Discord; wired to the scanner cadence + `polyarb settle`. | **Stretch (open):** flag *earlier* — the moment the live book makes positive settlement unreachable (needs a held position; overlaps **E3**, Phase-5). |
| **E3** | ✶ | Phase-5 | **No live position monitor / edge-evaporation alert.** "Earlier, when it can't get positive anymore" assumes we hold a position and watch its book — an execution-side feature. | Defer to Phase 5: watch the live book of an open position; alert when the edge has evaporated. Depends on actually holding (or paper-trading) positions. |
| **E4** | §5, C | DEFERRED | **No edge-vs-luck test for probabilistic schemes.** Permutation / p-test to tell a real statistical edge from luck. Caveat: **we take no probabilistic bets yet** (§5 off, C2 deferred) → nothing to test until real or paper-traded directional bets exist. | After E1 + recorded directional bets: run permutation/bootstrap tests on realized P&L (is mean > 0 beyond chance?). Until then it would test an empty sample. Depends on **E1**; pairs with **§5/C2**. |

---

## Roadmap — ordered execution plan (updated 2026-06-30)

**State:** read-only **WebSocket-first** monitor live in Docker — sensible tier (30 bps / $50),
600-market coverage, hardened container. Streaming is now the default (R1–R8 shipped + committee
+ live-verified); REST poll is the resync/backup. Diagnostics + coverage-widening shipped; recon
done. Penny/small-edge tier **deferred** (see "Strategy direction" above — recon-killed for now).
Remaining streaming polish is non-blocking (Tier D-ws). Work the items below in order.

1. **Notifier wiring (Discord)** — *built.* `DiscordNotifier` (formatted embed) shipped; set
   `NOTIFIER=discord` + `NOTIFIER_URL=<channel webhook>` in the compose env so real opps actually
   alert (spec's "→ alert" is otherwise silent). Pending only Jonathan's Discord webhook URL.
   (ntfy/telegram deferred — fall back to `none`.)
2. **Dependency-relation workflow** — *the big near-term build.* Auto-**propose** candidate
   relations from market *structure* (temporal/numeric ladders, nesting DAGs — never free-text),
   **verify** each (resolution-fingerprint + adversarial committee hunting an A∧¬B scenario), then
   **register only verified** ones. Activates the dormant dependency detector with no manual
   curation. Resolves **D1** (fingerprint policy) as part of it. Gate + committee before commit.
3. **Websocket streaming — SHIPPED AS THE DEFAULT (2026-07-01), verified live in Docker.**
   In-memory books from deltas: real-time detection + far less CPU/IO than re-fetching books/pass;
   the only way to catch instant transients. **All of R1–R8 landed** — see the WS-default/R2/R5/R6/R8
   rows in the Shipped table above. Cache (1), runner (2), and scanner integration (3, the
   trigger→REST-confirm barrier) are all wired; `streaming_enabled=True` is the default and the REST
   poll is the resync/backup. Committee-reviewed (all SAFE, none blocking) + 2× worktree bug-hunt;
   the historical R1–R8 design + committee verdict below is retained for provenance. **Remaining
   streaming polish is in Tier D-ws below** (non-blocking scaling/observability items).

   ### Websocket phase-3 design — committee verdict (2026-07-01)
   A 3-lens Opus committee (data-integrity · execution-realism · operational) **unanimously**
   concluded: **do NOT detect-and-emit directly off the streamed cache.** Treat `StreamingBooks`
   as a low-latency *trigger* / candidate generator, and gate emission behind a **REST-confirm
   barrier**. Rationale: a single dropped delta fabricates a phantom **instant complement** across
   the `YES+NO=1` knife-edge (tagged OBJECTIVE → exempt from the risk gate → the one false
   positive nothing catches); the integrity check validates only top-of-book *price* (not size,
   not depth), so silent divergence is invisible for up to one resync interval and inflates
   `executable_size`/notional/rank. Bug-hunt + committee already landed the *now*-fixable cache
   bugs (null-safety, best=0 sentinel both directions, `seed()` last-write-wins).

   **Phase-3 design requirements (must hold before `streaming_enabled=true` is safe):**
   - **R1 — REST-confirm before emit.** **Barrier BUILT** (`engine/confirm.py`,
     `confirm_candidate`): re-fetches a candidate's exact legs, re-runs its detector against fresh
     books, returns the authoritative fresh opp only if the same leg-signature (under≠over,
     basket≠dual) still holds — else None. Standalone + tested (`tests/test_confirm.py`).
     **Remaining: wire it into the scan loop** (source books from the cache, run detectors →
     candidates, confirm each before emit) — couples with R3/R7 below.
   - **R2 — per-token wall-clock freshness guard** (time since last applied delta *or* successful
     resync), distinct from the book's last-change `timestamp_ms`. Streaming staleness window in
     **seconds**, not `max_book_age_s=900`. Do NOT reuse `_fresh_books`-by-last-change for the
     streamed path (it both keeps a 15-min-dead feed and drops valid quiescent books).
   - **R3 — detect on a fixed cadence over a cache snapshot**, not per-delta; coarsen/replace the
     dedupe cost-bucket for streaming (else bucket-flap re-emits the same opp + ephemeral-edge spam).
   - **R4 — filter/rank on the conservative `decision_size`** — **SATISFIED**: the C1-atom-use
     decision (2026-07-01) already routes the `MIN_NOTIONAL` gate and the $-rank through
     `Opportunity.decision_size` globally, so the streamed path inherits it. A missed deep delta
     can no longer silently inflate the gated/ranked size.
   - **R5 — stream-stall watchdog.** A connected-but-quiet WS is undetectable today (degrades to a
     60s poll, ~12× staler than the 5s REST path, with no alarm; the D7 heartbeat stays green).
     Track `last_message` monotonic; on a gap force-reconnect + immediate resync + a metric.
   - **R6 — resubscription + cache eviction.** The runner subscribes to a fixed `token_ids`; the
     scanner re-discovers each pass. Diff the set per discovery, dynamic subscribe/unsubscribe
     (API_NOTES §WS), and evict dropped tokens (else silent misses on new markets + unbounded
     memory + wasted resync budget on resolved tokens).
   - **R7 — single shared `ClobClient`/limiter.** Each client builds its own `/book` token bucket
     (`base.py`); a separate streaming client doubles the real rate → 429s. Share one limiter, and
     when streaming, the scanner must read from the cache instead of re-fetching the global set.
   - **R8 — streaming metrics + stream-aware healthcheck:** `ws_last_message_timestamp_seconds`,
     `ws_reconnects_total`, `ws_resyncs_total`, `ws_resync_errors_total`, cache `token_count`,
     `skip_count`. The D7 healthcheck must not certify healthy while the WS is dead.

   **Streaming backlog (smaller / later):**
   - **Recompute-and-compare WS hash** locally on every delta (if the WS `hash` is a deterministic
     content hash — verify the algorithm vs API_NOTES). Highest-leverage integrity upgrade: turns
     the up-to-60s blind window into instant divergence detection; would relax R1's necessity.
   - Explicit WS `aclose()` on shutdown (cleanliness; currently relies on async-gen finalization).
   - Decide whether the streamed path keys staleness off last-change vs resync/fetch time (R2).
   - **Phase-5:** streaming gives a *weaker* execution guarantee on silent WS degrade — execution
     must REST-confirm + send marketable-limit orders that fail closed if the level is gone.
   - **NON-ISSUES (committee-confirmed, no action):** read-only / net-of-fees invariants intact;
     hash-history eviction is conservative-by-design; reconnect backoff is sound; the startup full
     resync fits the `/book` budget.
4. **False-positive hardening** — *partially shipped* (A3-quiescence extreme-spread predicate, D2
   yes-index, D6, F2, D7-heartbeat — parallel-worktree batch). Remaining: A3 hash-revert,
   D2-residual, A1-stale, per-leg min-order-size (D5), A1-riskwt, M3-feefloor. Quality now, *and*
   the prerequisite for revisiting the small-edge tier.
5. **Realized-outcome ledger (E1) → guaranteed-slip alarm (E2)** — the evaluation layer (did
   "guaranteed" really pay?). Its own workstream.

**Deferred:** small-edge tier (needs #3+#4+execution), **C2** (probabilistic ranking), **§5**
(opt-in/off), **E4** (no probabilistic bets yet). **Desk decisions:** B2′-num and A2-void
**DECIDED 2026-07-01** (committee → Shipped table); **D1** deferred into the dependency-relation
workflow (#2), leaning attestation.

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
